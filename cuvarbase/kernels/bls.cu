#include <stdio.h>
#define RESTRICT __restrict__
#define CONSTANT const
#define MIN_W 1E-5
//{CPP_DEFS}

__device__ int get_id(){
	return blockIdx.x * blockDim.x + threadIdx.x;
}

__device__ int mod(int a, int b){
	int r = a % b;
	return (r < 0) ? r + b : r;
}

__global__ void bin_and_phase_fold(float *t, float *yw, float *w,
									float *yw_bin, float *w_bin, float freq,
									int ndata, int nbins){
	int i = get_id();

	if (i < ndata){
		float W = w[i];
		float YW = yw[i];

		float phi = t[i] * freq;
		phi -= floorf(phi);

		int b = ((int)floorf(phi * nbins));

		atomicAdd(&(yw_bin[b]), YW);
		atomicAdd(&(w_bin[b]), W);
	}
}


// bls for a single frequency (phase folded and binned data)
// needs nbins threads, nbins data
__global__ void binned_bls(float *yw, float *w, float *bls, int nbins,
								float qmin, float qmax){
	int i = get_id();

	if (i < nbins){

		// initialize to 0
		bls[i] = 0.f;

		float wtot = 0.f;
		float ybar = 0.f;
		float p;

		int jmin = __float2int_rd(qmin * nbins);
		int jmax = __float2int_ru(qmax * nbins);

		for (int k = i; k < i + jmin; k++){
			wtot += w[k % nbins];
			ybar += yw[k % nbins];
		}

		for (int j = jmin + i; j < jmax + i; j++){
			wtot += w[j%nbins];
			ybar += yw[j%nbins];

			if (wtot < MIN_W)
				continue;

			p = ybar * ybar / (wtot * (1 - wtot));

			if (p > bls[i])
				bls[i] = p;
		}
	}
}



__global__ void bin_and_phase_fold_bst(float *t, float *yw, float *w,
									float *yw_bin, float *w_bin, float freq,
									int ndata, int nbins0, int nbinsf){
	int i = get_id();

	if (i < ndata){
		float W = w[i];
		float YW = yw[i];

		float phi = t[i] * freq;
		phi -= floorf(phi);

		for(int nb = nbins0; nb <= nbinsf; nb *= 2){
			int b = ((int) floorf(phi * nb)) + (nb - nbins0);
			atomicAdd(&(yw_bin[b]),YW);
			atomicAdd(&(w_bin[b]), W);
		}
	}
}

// bls for a single frequency (phase folded and binned data)
// needs nfreqs *  (2 * nbinsf - nbins0) threads
__global__ void binned_bls_bst(float *yw, float *w, float *bls, int n){
	int i = get_id();

	if (i < n){
		float wtot = w[i];
		float ybar = yw[i];

		bls[i] = (wtot > MIN_W && wtot < 1 - MIN_W) ?
					ybar * ybar / (wtot * (1.f - wtot)) : 0.f;

		if (bls[i] > 1)
			printf("ybar = %e, wtot = %e, ybar^2 = %e, wtot * (1 - wtot) = %e\n", ybar, wtot, ybar * ybar, wtot * (1 - wtot));
	}
}

__global__ void store_best_sols(int *argmaxes, float *best_phi, float *best_q,
	                            int nbins0, int nbinsf, int noverlap, 
	                            float alpha, int nfreq, int freq_offset){

	int i = get_id();

	if (i < nfreq){
		int imax = argmaxes[i];
		float dphi = 1.f / noverlap;
		int nb = nbins0;
		float x = 1.f;
		int offset = 0;

		while(offset + noverlap * nb < imax){
			x *= alpha;
			offset += noverlap * nb;
			nb = (int) (x * nbins0);
		}

		float q = 1.f / nb;
		int s = (imax - offset) / nb;

		int jphi = (imax - offset) - s * nb;
		
		float phi = (q * (jphi + s * dphi) - 0.5 * (1 - q));

		phi -= floorf(phi);

		best_phi[i + freq_offset] = phi;
		best_q[i + freq_offset] = q;
	}
}


// needs ndata * nfreq threads
// noverlap -- number of overlapped bins (noverlap * (1 / q) total bins)
// alpha -- logarithmic spacing for q; q_i = alpha^i q_0
__global__ void bin_and_phase_fold_bst_multifreq(float *t, float *yw, float *w,
									float *yw_bin, float *w_bin, float *freqs,
									int ndata, int nfreq, int nbins0, int nbinsf,
									int freq_offset, int noverlap, float alpha,
									int nbins_tot){
	int i = get_id();

	if (i < ndata * nfreq){
		int i_data = i % ndata;
		int i_freq = i / ndata;

		int offset = i_freq * nbins_tot * noverlap;

		float W = w[i_data];
		float YW = yw[i_data];

		// get phase [0, 1)
		float phi = t[i_data] * freqs[i_freq + freq_offset];
		phi -= floorf(phi);

		float dphi = 1.f / noverlap;
		int nbtot = 0;

		// iterate through bins (logarithmically spaced)
		for(float x = 1.f; ((int) (x * nbins0)) <= nbinsf; x *= alpha){
			int nb = (int) (x * nbins0);

			// iterate through offsets [ 0, 1./sigma, ..., (sigma - 1) / sigma ]
			for (int s = 0; s < noverlap; s++){
				int b = mod((int) floorf(phi * nb - s * dphi), nb)
							+ s * nb + noverlap * nbtot + offset;

				atomicAdd(yw_bin + b, YW);
				atomicAdd( w_bin + b, W);
			}

			nbtot += nb;
		}
	}
}


__global__ void reduction_max(float *arr, int *arr_args, int nfreq, int nbins, int stride,
                              float *block_max, int *block_arg_max, int offset, int init){


	__shared__ float partial_max[BLOCK_SIZE];
	__shared__ int partial_arg_max[BLOCK_SIZE];

	int id = blockIdx.x * blockDim.x + threadIdx.x;

	int nblocks_per_freq = gridDim.x / nfreq;
	int nthreads_per_freq = blockDim.x * nblocks_per_freq;


	//	freq_no / b
	//			----block 1 -----       ----- block N ------------------------
	//		  0 | 0 1 2 .. B - 1 | ... | (N - 1)B, ... , ndata, ..., N * B - 1|
	//
	//			---block N + 1---       ---- block 2N ------------------------
	//		  1 | 0 1 2 .. B - 1 | ... | (N - 1)B, ... , ndata, ..., N * B - 1|
	//			...
	//
	//			---(nf - 1)N ----       --- nf * N ---
	//   nf - 1 | ..             | ... |             |

	int fno = id / nthreads_per_freq;
	int b   = id % nthreads_per_freq;

	// read part of array from global memory into shared memory
	partial_max[threadIdx.x] = (fno < nfreq && b < nbins) ?
	                                 arr[fno * stride + b] : -1.f;

	partial_arg_max[threadIdx.x] = (fno < nfreq && b < nbins) ?
									(
										(init == 1) ?
											b : arr_args[fno * stride + b]
									) : -1;

	__syncthreads();

	float m1, m2;

	// reduce to find max of shared memory array
	for(int s = blockDim.x / 2; s > 0; s /= 2){
		if(threadIdx.x < s){
			m1 = partial_max[threadIdx.x];
			m2 = partial_max[threadIdx.x + s];

			partial_max[threadIdx.x] = (m1 > m2) ? m1 : m2;

			partial_arg_max[threadIdx.x] = (m1 > m2) ?
			 						partial_arg_max[threadIdx.x] :
			 						partial_arg_max[threadIdx.x + s];
		}

		__syncthreads();
	}

	// store partial max back into global memory
	if (threadIdx.x == 0 && fno < nfreq){
		int i = (gridDim.x == nfreq) ? 0 :
			fno * stride - fno * nblocks_per_freq;

		i += blockIdx.x + offset;

		block_max[i] = partial_max[0];
		block_arg_max[i] = partial_arg_max[0];
	}
}
