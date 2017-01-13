#!/usr/bin/env python2

""" jointly infers heterozygosity and error rate from stacked sequences """

from __future__ import print_function

import scipy.stats
import scipy.optimize
import numpy as np
import numba
import itertools
import datetime
import time
import gzip
import os

from ipyrad.assemble.cluster_within import get_quick_depths

from collections import Counter
from util import *


# pylint: disable=E1101
# pylint: disable=W0212
# pylint: disable=W0142
# pylint: disable=C0301



def likelihood1(errors, bfreqs, ustacks):
    """
    Probability homozygous. """
    ## make sure base_frequencies are in the right order
    #print uniqstackl.sum()-uniqstack, uniqstackl.sum(), 0.001
    #totals = np.array([ustacks.sum(axis=1)]*4).T
    totals = np.array([ustacks.sum(axis=1)]*4).T
    prob = scipy.stats.binom.pmf(totals-ustacks, totals, errors)
    lik1 = np.sum(bfreqs*prob, axis=1)
    return lik1



@numba.jit(nopython=True)
def nblik2_build(err, ustacks):

    ## store
    ret = np.empty(ustacks.shape[0])

    ## fill for pmf later 
    tots = np.empty((ustacks.shape[0], 1))
    twos = np.empty((ustacks.shape[0], 6))
    thrs = np.empty((ustacks.shape[0], 6, 2))

    ## fill big arrays
    for idx in xrange(ustacks.shape[0]):

        ust = ustacks[idx]
        tot = ust.sum()
        tots[idx] = tot

        ## fast filling of arrays
        i = 0
        for jdx in xrange(4):
            for kdx in xrange(4):
                if jdx < kdx:
                    twos[idx][i] = tot - ust[jdx] - ust[kdx]
                    thrs[idx][i] = ust[jdx], ust[jdx] + ust[kdx]
                    i += 1

    return tots, twos, thrs



def lik2_calc(err, one, tots, twos, thrs, four):

    ## calculate twos
    #_twos = np.array([scipy.stats.binom.pmf(\
    #        twos[i], tots[i], 0.5) for i in xrange(tots.shape[0])])
    _twos = scipy.stats.binom.pmf(twos, tots, 0.5)

    ## calculate threes
    dim = thrs.shape
    _thrs = thrs.reshape(thrs.shape[0] * thrs.shape[1], thrs.shape[2])
    _thrs = scipy.stats.binom.pmf(_thrs[:, 0], _thrs[:, 1], (2.*err) / 3.)
    _thrs = _thrs.reshape(thrs.shape[0], 6)

    ## calculate return sums
    return np.sum(one * _twos * (_thrs / four), axis=1)



## global
def nlikelihood2(errors, bfreqs, ustacks):
    one = [2. * bfreqs[i] * bfreqs[j] for i, j in itertools.combinations(range(4), 2)]
    four = 1. - np.sum(bfreqs**2) 
    tots, twos, thrs = nblik2_build(errors, ustacks)
    res2 = lik2_calc(errors, one, tots, twos, thrs, four)
    return res2



def olikelihood2(errors, bfreqs, ustacks):
    """probability of heterozygous"""
    
    returns = np.empty([len(ustacks)])
    four = 1.-np.sum(bfreqs**2)

    for idx in xrange(ustacks.shape[0]):
        ustack = ustacks[idx]
        spair = np.array(list(itertools.combinations(ustack, 2)))
        bpair = np.array(list(itertools.combinations(bfreqs, 2)))
        one = 2.*bpair.prod(axis=1)
        tot = ustack.sum()
        atwo = tot - spair[:, 0] - spair[:, 1]
        two = scipy.stats.binom.pmf(atwo, tot, (2.*errors)/3.)
        three = scipy.stats.binom.pmf(\
                    spair[:, 0], spair.sum(axis=1), 0.5)

        returns[idx] = np.sum(one*two*(three/four))
    return np.array(returns)


## more verbose and slow form of the function above
def liketest2(errors, bfreqs, ustack):
    """probability of heterozygous"""

    fullsum = 0
    for idx in xrange(4):
        subsum = 0
        for jdx in xrange(4):
            one = 2. * bfreqs[idx] * bfreqs[jdx]
            tot = ustack.sum()
            two = scipy.stats.binom.pmf(tot - ustack[idx] - ustack[jdx], 
                                        tot, (2.*errors)/3.)
            three = scipy.stats.binom.pmf(ustack[idx], 
                                          ustack[idx] + ustack[jdx], 0.5)
            four = 1 - np.sum(bfreqs**2)
            subsum += one * two * (three / four)
        fullsum += subsum
    return fullsum



def oget_diploid_lik(pstart, bfreqs, ustacks, counts):
    """ Log likelihood score given values [H,E] """
    hetero, errors = pstart
    if (hetero <= 0.) or (errors <= 0.):
        score = np.exp(100)
    else:
        ## get likelihood for all sites
        lik1 = (1.-hetero) * likelihood1(errors, bfreqs, ustacks)
        lik2 = (hetero) * olikelihood2(errors, bfreqs, ustacks)
        liks = lik1 + lik2
        logliks = np.log(liks[liks > 0]) * counts[liks > 0]
        score = -logliks.sum()
    return score



def nget_diploid_lik(pstart, bfreqs, ustacks, counts):
    """ Log likelihood score given values [H,E] """
    hetero, errors = pstart
    if (hetero <= 0.) or (errors <= 0.):
        score = np.exp(100)
    else:
        ## get likelihood for all sites
        lik1 = (1.-hetero) * likelihood1(errors, bfreqs, ustacks)
        lik2 = (hetero) * nlikelihood2(errors, bfreqs, ustacks)
        liks = lik1 + lik2
        logliks = np.log(liks[liks > 0]) * counts[liks > 0]
        score = -logliks.sum()
    return score



def get_haploid_lik(errors, bfreqs, ustacks, counts):
    """ Log likelihood score given values [E]. This can be written to run much
    faster by executing across the whole array, and/or by also in parallel """
    hetero = 0.
    ## score terribly if below 0
    if errors <= 0.:
        score = np.exp(100)
    else:
        ## get likelihood for all sites
        lik1 = ((1.-hetero)*likelihood1(errors, bfreqs, ustacks)) 
        lik2 = (hetero)*likelihood2(errors, bfreqs, ustacks)
        liks = lik1+lik2
        logliks = np.log(liks[liks > 0])*counts[liks > 0]
        score = -logliks.sum()
    return score



def recal_hidepth(data, sample):
    """
    if mindepth setting were changed then 'clusters_hidepth' needs to be 
    recalculated. Check and recalculate if necessary.
    """
    ## the minnest depth
    majrdepth = data.paramsdict["mindepth_majrule"]
    statdepth = data.paramsdict["mindepth_statistical"]    

    ## if nothing changes return existing maxlen value
    maxlen = data._hackersonly["max_fragment_length"]

    ## if coming from older version of ipyrad this attr is new
    if not hasattr(sample.stats_dfs.s3, "hidepth_min"):
        sample.stats_dfs.s3["hidepth_min"] = data.paramsdict["mindepth_majrule"]

    ## if old value not the same as current value then recalc
    if 1: #not sample.stats_dfs.s3["hidepth_min"] == majrdepth:
        LOGGER.info(" mindepth setting changed: recalculating clusters_hidepth and maxlen")
        ## get arrays of data
        maxlens, depths = get_quick_depths(data, sample)

        ## calculate how many are hidepth
        hidepths = depths >= majrdepth
        stathidepths = depths >= statdepth
        
        keepmj = depths[hidepths]
        keepst = depths[stathidepths]

        ## set assembly maxlen for sample
        statlens = maxlens[stathidepths]        
        statlen = int(statlens.mean() + (2.*statlens.std()))        

        LOGGER.info("%s %s %s", maxlens.shape, maxlens.mean(), maxlens.std())
        maxlens = maxlens[hidepths]
        maxlen = int(maxlens.mean() + (2.*maxlens.std()))

        ## saved stat values are for majrule min
        sample.stats["clusters_hidepth"] = keepmj.shape[0]
        sample.stats_dfs.s3["clusters_hidepth"] = keepmj.shape[0]        

    return sample, keepmj.shape[0], maxlen, keepst.shape[0], statlen



def stackarray(data, sample, subloci):
    """ 
    Stacks clusters into arrays
    """

    ## only use clusters with depth > mindepth_statistical for param estimates
    sample, _, _, nhidepth, maxlen = recal_hidepth(data, sample)

    ## get clusters file    
    clusters = gzip.open(sample.files.clusters)
    pairdealer = itertools.izip(*[iter(clusters)]*2)

    ## we subsample, else use first 10000 loci.
    dims = (nhidepth, maxlen, 4)
    stacked = np.zeros(dims, dtype=np.uint64)

    ## don't use sequence edges / restriction overhangs
    cutlens = [None, None]
    try:
        cutlens[0] = len(data.paramsdict["restriction_overhang"][0])
        cutlens[1] = maxlen - len(data.paramsdict["restriction_overhang"][1])
    except TypeError:
        pass
    #LOGGER.info("cutlens: %s", cutlens)

    ## fill stacked
    nclust = 0
    done = 0
    while not done:
        try:
            done, chunk = clustdealer(pairdealer, 1)
        except IndexError:
            raise IPyradError("  clustfile formatting error in %s", chunk)

        if chunk:
            piece = chunk[0].strip().split("\n")
            names = piece[0::2]
            seqs = piece[1::2]
            ## pull replicate read info from seqs
            reps = [int(sname.split(";")[-2][5:]) for sname in names]
            
            ## double reps if the read was fully merged... (TODO: Test this!)
            #merged = ["_m1;s" in sname for sname in names]
            #if any(merged):
            #    reps = [i*2 if j else i for i, j in zip(reps, merged)]

            ## get all reps
            sseqs = [list(seq) for seq in seqs]
            arrayed = np.concatenate(
                          [[seq]*rep for seq, rep in zip(sseqs, reps)])
            
            ## enforce minimum depth for estimates
            if arrayed.shape[0] >= data.paramsdict["mindepth_statistical"]:
                ## remove edge columns and select only the first 500 
                ## derep reads, just like in step 5
                arrayed = arrayed[:500, cutlens[0]:cutlens[1]]
                ## remove cols that are pair separator
                arrayed = arrayed[:, ~np.any(arrayed == "n", axis=0)]
                ## remove cols that are all Ns after converting -s to Ns
                arrayed[arrayed == "-"] = "N"
                arrayed = arrayed[:, ~np.all(arrayed == "N", axis=0)]
                ## store in stacked dict

                catg = np.array(\
                    [np.sum(arrayed == i, axis=0) for i in list("CATG")], 
                    dtype=np.uint64).T

                stacked[nclust, :catg.shape[0], :] = catg
                nclust += 1

    ## drop the empty rows in case there are fewer loci than the size of array
    newstack = stacked[stacked.sum(axis=2) > 0]
    assert not np.any(newstack.sum(axis=1) == 0), "no zero rows"
    clusters.close()

    return newstack



def optim(data, sample, subloci):
    """ func scipy optimize to find best parameters"""

    ## get array of all clusters data
    stacked = stackarray(data, sample, subloci)
    #maxsz = stacked.shape[0]
    #stacked = stacked[np.random.randint(0, min(subloci, maxsz), maxsz)]

    ## get base frequencies
    bfreqs = stacked.sum(axis=0) / float(stacked.sum())
    #bfreqs = bfreqs**2
    #LOGGER.debug(bfreqs)
    if np.isnan(bfreqs).any():
        raise IPyradWarningExit(" Bad stack in getfreqs; {} {}"\
               .format(sample.name, bfreqs))

    ## put into array, count array items as Byte strings
    tstack = Counter([j.tostring() for j in stacked])

    ## get keys back as arrays and store vals as separate arrays
    ustacks = np.array([np.fromstring(i, dtype=np.uint64) \
                        for i in tstack.iterkeys()])

    ## make bi-allelic only
    #tris = np.where(np.sum(ustacks > 0, axis=1) > 2)
    #for tri in tris:
    #    minv = np.min(ustacks[tri][ustacks[tri] > 0])
    #    delv = np.where(ustacks[tri] == minv)[0][0]
    #    ustacks[tri, delv] = 0

    counts = np.array(tstack.values())
    ## cleanup    
    del tstack


    ## if data are haploid fix H to 0
    if int(data.paramsdict["max_alleles_consens"]) == 1:
        pstart = np.array([0.001], dtype=np.float64)
        hetero = 0.
        errors = scipy.optimize.fmin(get_haploid_lik, pstart,
                                    (bfreqs, ustacks, counts),
                                     disp=False,
                                     full_output=False)
    ## or do joint diploid estimates
    else:
        pstart = np.array([0.01, 0.001], dtype=np.float64)
        hetero, errors = scipy.optimize.fmin(nget_diploid_lik, pstart,
                                            (bfreqs, ustacks, counts), 
                                            maxfun=50, 
                                            maxiter=50,
                                            disp=False,
                                            full_output=False)
    return hetero, errors



def run(data, samples, subloci, force, ipyclient):
    """ calls the main functions """

    ## speed hack == use only the first 2000 high depth clusters for estimation.
    ## based on testing this appears sufficient for accurate estimates
    ## the default is set in assembly.py

    # if haploid data
    if data.paramsdict["max_alleles_consens"] == 1:
        print("  Applying haploid-based test (infer E with H fixed to 0).")

    submitted_args = []
    ## if sample is already done skip
    for sample in samples:
        if not force:
            if sample.stats.state >= 4:
                print("    skipping {}; ".format(sample.name)+\
                      "already estimated. Use force=True to overwrite.")
            elif sample.stats.state < 3:
                print("    skipping {}; ".format(sample.name)+\
                      "not clustered yet. Run step3() first.")
            else:
                submitted_args.append([sample, subloci])
        else:
            if sample.stats.state < 3:
                print("    "+sample.name+" not clustered. Run step3() first.")
            elif sample.stats.clusters_hidepth < 2:
                print("    skipping {}. Too few high depth reads ({}). "\
                      .format(sample.name, sample.stats.clusters_hidepth))
            else:
                submitted_args.append([sample, subloci])

    if submitted_args:    
        ## submit jobs to parallel client
        submit(data, submitted_args, ipyclient)



def submit(data, submitted_args, ipyclient):
    """ 
    Sends jobs to engines and cleans up failures. Print progress. 
    """

    ## first sort by cluster size
    submitted_args.sort(key=lambda x: x[0].stats.clusters_hidepth, reverse=True)
                                           
    ## send all jobs to a load balanced client
    lbview = ipyclient.load_balanced_view()
    jobs = {}
    for sample, subloci in submitted_args:
        ## stores async results using sample names
        jobs[sample.name] = lbview.apply(optim, *(data, sample, subloci))

    ## dict to store cleanup jobs
    start = time.time()

    ## wrap in a try statement so that stats are saved for finished samples.
    ## each job is submitted to cleanup as it finishes
    try:
        kbd = 0
        ## wait for jobs to finish
        while 1:
            fin = [i.ready() for i in jobs.values()]
            elapsed = datetime.timedelta(seconds=int(time.time() - start))
            progressbar(len(fin), sum(fin), 
                " inferring [H, E]      | {} | s4 |".format(elapsed))
            time.sleep(0.1)
            if len(fin) == sum(fin):
                print("")
                break

        ## cleanup
        for job in jobs:
            if jobs[job].successful():
                hest, eest = jobs[job].result()
                sample_cleanup(data.samples[job], hest, eest)
            else:
                LOGGER.error("  Sample %s failed with error %s", 
                             job, jobs[job].exception())
                raise IPyradWarningExit("  Sample {} failed step 4"\
                                        .format(job))

    except KeyboardInterrupt as kbd:
        pass

    finally:
        assembly_cleanup(data)
        if kbd:
            raise KeyboardInterrupt



def sample_cleanup(sample, hest, eest):
    """ 
    Stores results to the Assembly object, writes to stats file, 
    and cleans up temp files 
    """
    ## sample summary assignments
    sample.stats.state = 4
    sample.stats.hetero_est = hest
    sample.stats.error_est = eest

    ## sample full assigments
    sample.stats_dfs.s4.hetero_est = hest
    sample.stats_dfs.s4.error_est = eest



def assembly_cleanup(data):
    """ cleanup assembly stats """
    ## Assembly assignment
    data.stats_dfs.s4 = data._build_stat("s4")#, dtype=np.float32)

    ## Update written file
    data.stats_files.s4 = os.path.join(data.dirs.clusts, 
                                       "s4_joint_estimate.txt")
    with open(data.stats_files.s4, 'w') as outfile:
        data.stats_dfs.s4.to_string(outfile)




if __name__ == "__main__":

    import ipyrad as ip

    ## get path to test dir/ 
    ROOT = os.path.realpath(
       os.path.dirname(
           os.path.dirname(
               os.path.dirname(__file__)
               )
           )
       )

    ## run test on RAD data1
    TEST = ip.load.load_assembly(os.path.join(\
                         ROOT, "tests", "test_pairgbs", "test_pairgbs"))
    TEST.step4(force=True)
    print(TEST.stats)

    TEST = ip.load.load_assembly(os.path.join(\
                         ROOT, "tests", "test_rad", "data1"))
    TEST.step4(force=True)
    print(TEST.stats)

    ## run test on messy data set
    #TEST = ip.load_assembly(os.path.join(ROOT, "tests", "radmess", "data1"))

    ## check if results are correct

    ## cleanup

