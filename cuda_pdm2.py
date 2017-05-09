#!/usr/bin/env python

import numpy as np 
import sys
import pycuda.driver as cuda
import pycuda.gpuarray as gpuarray
from pycuda.compiler import SourceModule
import resource 
from gpubatch import GPUAsyncProcess

def autofrequency(t, nyquist_factor=5, samples_per_peak=5,
                      minimum_frequency=None, maximum_frequency = None):
    """
    Determine a suitable frequency grid for data.

    Note that this assumes the peak width is driven by the observational
    baseline, which is generally a good assumption when the baseline is
    much larger than the oscillation period.
    If you are searching for periods longer than the baseline of your
    observations, this may not perform well.

    Even with a large baseline, be aware that the maximum frequency
    returned is based on the concept of "average Nyquist frequency", which
    may not be useful for irregularly-sampled data. The maximum frequency
    can be adjusted via the nyquist_factor argument, or through the
    maximum_frequency argument.

    Parameters
    ----------
    samples_per_peak : float (optional, default=5)
        The approximate number of desired samples across the typical peak
    nyquist_factor : float (optional, default=5)
        The multiple of the average nyquist frequency used to choose the
        maximum frequency if maximum_frequency is not provided.
    minimum_frequency : float (optional)
        If specified, then use this minimum frequency rather than one
        chosen based on the size of the baseline.
    maximum_frequency : float (optional)
        If specified, then use this maximum frequency rather than one
        chosen based on the average nyquist frequency.

    Returns
    -------
    frequency : ndarray or Quantity
        The heuristically-determined optimal frequency bin
    """
    baseline = max(t) - min(t)
    n_samples = len(t)

    df = 1. / (baseline * samples_per_peak)

    if minimum_frequency is not None:
        nf0 = min([ 1, np.floor(minimum_frequency / df) ])
    else:
        nf0 = 1

    if maximum_frequency is not None:
        Nf = int(np.ceil(maximum_frequency / df - nf0))
    else:
        Nf = int(0.5 * samples_per_peak * nyquist_factor * n_samples)

    return df * (nf0 + np.arange(Nf))


def dphase(dt, freq):
    dph = dt * freq - np.floor(dt * freq)
    dph_final = dph if dph < 0.5 else 1 - dph
    return dph_final

def var_tophat(t, y, w, freq, dphi):
    var = 0.
    for i, (T, Y, W) in enumerate(zip(t, y, w)):
        mbar = 0.
        wtot = 0.
        for j, (T2, Y2, W2) in enumerate(zip(t, y, w)):
            dph = dphase(abs(T2 - T), freq)
            if dph < dphi:
                mbar += W2 * Y2
                wtot += W2
        
        var += W * (Y - mbar / wtot)**2
        
    return var

def var_binned(t, y, w, freq, nbins, linterp=True):
    bin_means = np.zeros(nbins)
    bin_vars = np.zeros(nbins)
    bin_wtots = np.zeros(nbins)
    wtot_skipped = 0.
    phase = (t * freq) % 1.0
    bins = [ int(p * nbins) for p in phase ]
    
    for i in range(nbins):
        bin_wtots[i] = max([ sum([ W for j, W in enumerate(w) if bins[j] == i ]), 1E-10 ])
        bin_means[i] = sum([ W * Y for j, (Y, W) in enumerate(zip(y, w)) if bins[j] == i ]) / bin_wtots[i]
        #print(i, bin_means[i] * bin_wtots[i], bin_wtots[i])

    def pred_y(p):
        bs = np.array([ int(P * nbins) for P in phase ])
        ypreds = []
        alphas = p * nbins - (b + 0.5) / nbins
        di = np.floor(alphas)
        bins0 = bs + di
        bins1 = bins0 + 1
        for P, bs in zip(p, bs):
            yprior = bin_means[b]
            if linterp:
                alpha = P * nbins - (b + 0.5) / nbins
                alpha = P * nbins - (B + 0.5) / nbins
                bin0 = B - 1 if alpha < 0 else B
                bin1 = B if alpha < 0 else B + 1

                if bin0 < 0: bin0 += nbins
                if bin1 >= nbins: bin1 -= nbins
                if alpha < 0: alpha += 1

                if bin_wtots[bin0] < 1E-9 or bin_wtots[bin1] < 1E-9:
                    wtsk += 
                    continue

            yprior = (1 - alpha) * bin_means[bin0] + alpha * bin_means[bin1]

        
    for i, (P, Y, W, B) in enumerate(zip(phase, y, w, bins)):
        #print(i, freq, t[i], P, Y, W, B)
        yprior = bin_means[B]
        if linterp:
            alpha = P * nbins - (B + 0.5) / nbins
            bin0 = B - 1 if alpha < 0 else B
            bin1 = B if alpha < 0 else B + 1

            if bin0 < 0: bin0 += nbins
            if bin1 >= nbins: bin1 -= nbins
            if alpha < 0: alpha += 1

            if bin_wtots[bin0] < 1E-9 or bin_wtots[bin1] < 1E-9:
                wtot_skipped += W
                continue
        
            yprior = (1 - alpha) * bin_means[bin0] + alpha * bin_means[bin1]

        bin_vars[B] += W * (Y - yprior)**2

    return sum(bin_vars) / (1. - wtot_skipped)


def binless_pdm_cpu(t, y, w, freqs, dphi=0.05):
    ybar = np.dot(w, y)
    var = np.dot(w, np.power(y - ybar, 2))
    return [ var / var_tophat(t, y, w, freq, dphi) for freq in freqs ]

def pdm2_cpu(t, y, w, freqs, nbins=30, linterp=True):
    ybar = np.dot(w, y)
    var = np.dot(w, np.power(y - ybar, 2))
    return [ var / var_binned(t, y, w, freq, nbins=nbins, linterp=linterp)  for freq in freqs ]

def pdm_async(stream, data_cpu, data_gpu, pow_cpu, function, dphi=0.05, block_size=128):
    t, y, yerr, freqs = data_cpu
    t_g, y_g, w_g, freqs_g, pow_g = data_gpu

    # constants
    nfreqs = np.int32(len(freqs))
    ndata  = np.int32(len(t))
    dphi   = np.float32(dphi)

    # kernel size
    grid_size = int(np.ceil(float(nfreqs) / block_size))
    grid = (grid_size, 1)
    block = (block_size, 1, 1)

    # weights + weighted variance
    weights = np.power(yerr, -2)
    weights/= np.sum(weights)
    ybar = np.dot(weights, y)
    var = np.float32(np.dot(weights, np.power(y - ybar, 2)))

    # transfer data
    w_g.set_async(weights.astype(np.float32), stream=stream)
    t_g.set_async(np.array(t, dtype=np.float32), stream=stream)
    y_g.set_async(np.array(y, dtype=np.float32), stream=stream)

    function.prepared_async_call(grid, block, stream,
                t_g.ptr, y_g.ptr, w_g.ptr, freqs_g.ptr, pow_g,
                ndata, nfreqs, dphi, var)
    
    cuda.memcpy_dtoh_async(pow_cpu, pow_g, stream)

    return pow_cpu

class PDMAsyncProcess(GPUAsyncProcess):
    def _compile_and_prepare_functions(self):
        self.module = SourceModule(open('pdm2.cu', 'r').read(), options=['--use_fast_math'])

        self.dtypes = [ np.intp, np.intp, np.intp, np.intp, np.intp, 
                      np.int32, np.int32, np.float32, np.float32 ]
        for function in [ 'pdm_tophat', 'pdm_gauss', 'pdm_binned_linterp']:
            self.prepared_functions[function] = \
                self.module.get_function(function).prepare(self.dtypes)

    def allocate(self, data):
        if len(data) > len(self.streams):
            self._create_streams(len(data) - len(self.streams))

        gpu_data, pow_cpus =  [], []

        for t, y, yerr, freqs in data:

            pow_cpu = cuda.aligned_zeros(shape=(len(freqs),), 
                                             dtype=np.float32, 
                                             alignment=resource.getpagesize()) 

            pow_cpu = cuda.register_host_memory(pow_cpu)

            t_g, y_g, w_g = tuple([gpuarray.zeros(len(t), dtype=np.float32) for i in range(3)])
            pow_g = cuda.mem_alloc(pow_cpu.nbytes)
            freqs_g = gpuarray.to_gpu(np.asarray(freqs).astype(np.float32))

            gpu_data.append((t_g, y_g, w_g, freqs_g, pow_g))
            pow_cpus.append(pow_cpu)
        return gpu_data, pow_cpus

    def run(self, data, gpu_data=None, pow_cpus=None, function='pdm_binned_linterp', **pdm_kwargs):
        if not function in self.prepared_functions:
            self._compile_and_prepare_functions()

        if pow_cpus is None or gpu_data is None:
            gpu_data, pow_cpus = self.allocate(data)

        streams = [ s for i, s in enumerate(self.streams) if i < len(data) ]
        func = self.prepared_functions[function]
        results = [ pdm_async(stream, cdat, gdat, pcpu, func, **pdm_kwargs) \
                          for stream, cdat, gdat, pcpu in \
                                  zip(streams, data, gpu_data, pow_cpus)]
        
        return results

if __name__ == '__main__':
    import matplotlib.pyplot as plt
    import pycuda.autoinit


    ndata = 50
    p_min = 0.2 # minimum period (days)
    year = 365.
    T = 1. * year    # baseline (years)
    oversampling = 3 # df = 1 / (o * T)
    batch_size = 1
    nlcs = 1 * batch_size
    block_size = 160

    # nominal number of frequencies needed
    Nf = int(oversampling * T / p_min)
    print Nf

    #Nf = 10
    sigma = 2
    noise_sigma = 0.1
    m=8


    rand = np.random.RandomState(100)
    signal_freqs = [ (1 + 0.1 * np.random.rand()) * 3 for i in range(nlcs) ]


    random_times = lambda N : T * np.sort(rand.rand(N))
    noise = lambda N : noise_sigma * rand.randn(N)
    omega = lambda freq : 2 * np.pi * freq
    phase = lambda : 2 * np.pi * rand.rand()
    
    random_signal = lambda X, frq : np.cos(omega(frq) * X - phase()) + noise(len(X))

    x = [ random_times(ndata) for i in range(nlcs) ]
    y = [ random_signal(X, freq) for X, freq in zip(x, signal_freqs) ]
    err = [ noise_sigma * np.ones_like(Y) for Y in y ]
    
    df = 1./(T * oversampling)
    freqs = df * (0.5 + np.arange(Nf))
    #freqs = np.array([ 3.0 ])
    data = [ (X, Y, ERR, freqs) for X, Y, ERR in zip(x, y, err) ]

    #plt.plot(freqs, P)
    #plt.show()
    #sys.exit()

    cuda.start_profiler()    
    pdm_proc = PDMAsyncProcess()
    results = pdm_proc.run(data)
    pdm_proc.finish()
    cuda.stop_profiler()

    w = np.power(err[0], -2)
    w /= sum(w)
    P = pdm2_cpu(x[0], y[0], w, freqs)
    plt.plot(freqs, results[0])
    plt.plot(freqs, P, color='k')
    plt.show()
    """
    f, ax = plt.subplots()
    for f0, (frq, p) in zip(signal_freqs, results):
        ax.plot(frq, p, alpha=0.3)
        ax.axvline(f0, ls=':', color='k')    
    plt.show()
    
    """
