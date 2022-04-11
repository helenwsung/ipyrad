#!/usr/bin/env python

"""Jointly estimate heterozygosity and error rate."""

from typing import TypeVar, Tuple
from pathlib import Path
from collections import Counter
from itertools import combinations
from loguru import logger
from scipy.optimize import minimize
import scipy.stats
import pandas as pd
import numpy as np
import numba

from ipyrad.assemble.base_step import BaseStep
from ipyrad.core.schema import Stats4
from ipyrad.core.progress_bar import AssemblyProgressBar
from ipyrad.assemble.utils import IPyradError
from ipyrad.assemble.clustmap_within_denovo_utils import iter_clusters

Assembly = TypeVar("Assembly")
Sample = TypeVar("Sample")
logger = logger.bind(name="ipyrad")


class Step4(BaseStep):
    """Run the step4 estimation """
    def __init__(self, data, force, quiet, ipyclient):
        super().__init__(data, 4, quiet, force)       
        self.haploid = data.params.max_alleles_consens == 1
        self.ipyclient = ipyclient
        self.lbview = self.ipyclient.load_balanced_view()

    def run(self):
        """Distribute optimization jobs and store results."""
        jobs = {}
        for sname, sample in self.samples.items():
            args = (self.data, sample, self.haploid)
            jobs[sname] = self.lbview.apply(optim2, *args)
        msg = "inferring [H, E]"
        prog = AssemblyProgressBar(jobs, msg, 4, self.quiet)
        prog.block()
        prog.check()

        # collect updated samples and save to JSON
        for sname, sample in prog.results.items():
            self.data.samples[sname] = sample
        self.data.save_json()

        # write to stats file
        statsdf = pd.DataFrame(
            index=sorted(self.data.samples),
            columns=["hetero_est", "error_est"],
        )
        for sname in self.data.samples:
            stats = self.data.samples[sname].stats_s4.dict()
            for i in statsdf.columns:
                statsdf.loc[sname, i] = stats[i]
        logger.info("\n" + statsdf.to_string())
        outfile = self.stepdir / "s4_joint_estimate.txt"
        with open(outfile, 'w', encoding="utf-8") as out:
            out.write(statsdf.to_string())


def optim2(data, sample, haploid):
    """Maximum likelihood optimization with scipy."""

    # get array of all clusters data: (maxclusts, maxlen, 4)
    stacked = get_stackarray(data, sample)

    # get base frequencies
    bfreqs = stacked.sum(axis=0) / float(stacked.sum())
    if np.isnan(bfreqs).any():
        raise IPyradError(
            f"Bad stack in getfreqs; {sample.name} {bfreqs}")

    # put into array, count array items as Byte strings
    tstack = Counter([j.tostring() for j in stacked])

    # get keys back as arrays and store vals as separate arrays
    ustacks = np.array(
        [np.frombuffer(i, dtype=np.uint64) for i in tstack.keys()]
    )
    counts = np.array(list(tstack.values()))

    # cleanup
    del tstack

    if haploid:
        # fit haploid model
        fit = minimize(
            get_haploid_loglik, 
            x0=(0.001,),
            args=(bfreqs, ustacks, counts),
            method="L-BFGS-B",
            bounds=[1e-10, 1.0],
        )
        hetero = 0.0
        error = fit.x[0]
    else:
        # fit haploid model
        fit = minimize(
            nget_diploid_loglik, 
            x0=(0.01, 0.001),
            args=(bfreqs, ustacks, counts),
            method="L-BFGS-B",            
            bounds=[(1e-10, 1.0), (1e-10, 1.0)],
        )
        hetero, error = fit.x

    sample.state = 4
    sample.stats_s4 = Stats4(
        hetero_est=hetero,
        error_est=error,
        min_depth_stat_during_step4=data.params.min_depth_statistical,
    )
    return sample

def get_haploid_loglik(errors, bfreqs, ustacks, counts):
    """Log likelihood score given values [E]."""
    hetero = 0.
    lik1 = ((1. - hetero) * likelihood1(errors, bfreqs, ustacks))
    liks = lik1
    logliks = np.log(liks[liks > 0]) * counts[liks > 0]
    score = -logliks.sum()
    return score

def nget_diploid_loglik(pstart, bfreqs, ustacks, counts):
    """Log likelihood score given values [H,E]"""
    hetero, errors = pstart
    lik1 = (1. - hetero) * likelihood1(errors, bfreqs, ustacks)
    lik2 = (hetero) * nlikelihood2(errors, bfreqs, ustacks)
    liks = lik1 + lik2
    logliks = np.log(liks[liks > 0]) * counts[liks > 0]
    score = -logliks.sum()
    return score

def likelihood1(errors, bfreqs, ustacks):
    """Probability homozygous."""
    ## make sure base_frequencies are in the right order
    # print uniqstackl.sum()-uniqstack, uniqstackl.sum(), 0.001
    # totals = np.array([ustacks.sum(axis=1)]*4).T
    totals = np.array([ustacks.sum(axis=1)] * 4).T
    prob = scipy.stats.binom.pmf(totals - ustacks, totals, errors)
    lik1 = np.sum(bfreqs * prob, axis=1)
    return lik1

def nlikelihood2(errors, bfreqs, ustacks):
    """Calls nblik2_build and lik2_calc for a given err."""
    one = [2. * bfreqs[i] * bfreqs[j] for i, j in combinations(range(4), 2)]
    four = 1. - np.sum(bfreqs**2)
    tots, twos, thrs = nblik2_build(ustacks)
    res2 = lik2_calc(errors, one, tots, twos, thrs, four)
    return res2


@numba.jit(nopython=True)
def nblik2_build(ustacks):
    """
    JIT'd function builds array that can be used to calc binom pmf
    """
    # fill for pmf later
    tots = np.empty((ustacks.shape[0], 1))
    twos = np.empty((ustacks.shape[0], 6))
    thrs = np.empty((ustacks.shape[0], 6, 2))

    # fill big arrays
    for idx in range(ustacks.shape[0]):

        ust = ustacks[idx]
        tot = ust.sum()
        tots[idx] = tot

        # fast filling of arrays
        i = 0
        for jdx in range(4):
            for kdx in range(4):
                if jdx < kdx:
                    twos[idx][i] = tot - ust[jdx] - ust[kdx]
                    thrs[idx][i] = ust[jdx], ust[jdx] + ust[kdx]
                    i += 1
    return tots, twos, thrs


def lik2_calc(err, one, tots, twos, thrs, four):
    """
    vectorized calc of binom pmf on large arrays
    """
    # calculate twos
    _twos = scipy.stats.binom.pmf(twos, tots, 0.5)

    # calculate threes
    _thrs = thrs.reshape(thrs.shape[0] * thrs.shape[1], thrs.shape[2])
    _thrs = scipy.stats.binom.pmf(_thrs[:, 0], _thrs[:, 1], (2. * err) / 3.)
    _thrs = _thrs.reshape(thrs.shape[0], 6)

    # calculate return sums
    return np.sum(one * _twos * (_thrs / four), axis=1)


def recal_hidepth_stat(data: Assembly, sample: Sample) -> Tuple[np.ndarray, int]:
    """Return a mask for cluster depths, and the max frag length.

    This is useful to run first to get a sense of the depths and lens
    given the current mindepth param settings.
    """
    # otherwise calculate depth again given the new mindepths settings.
    depths = []   # read depth: sum of 'sizes'
    clens = []    # lengths of clusters
    for clust in iter_clusters(sample.files.clusters, gzipped=True):
        names = clust[::2]
        sizes = [int(i.split(";")[-2][5:]) for i in names]
        depths.append(sum(sizes))
        clens.append(len(clust[1].strip()))
    clens, depths = np.array(clens), np.array(depths)

    # get mask of clusters that are hidepth
    stat_mask = depths >= data.params.min_depth_statistical

    # get frag lenths of clusters that are hidepth
    lens_above_st = clens[stat_mask]

    # calculate frag length from hidepth lens
    try:       
        maxfrag = int(4 + lens_above_st.mean() + (2. * lens_above_st.std()))
    except Exception as inst:
        raise IPyradError(
            "No clusts with depth sufficient for statistical basecalling. "
            f"I recommend you branch to drop this sample: {sample.name}"
            ) from inst
    return stat_mask, maxfrag


def get_stackarray(data: Assembly, sample: Sample, size: int=10_000) -> np.ndarray:
    """Stacks clusters into arrays using at most 10K clusters.

    Uses maxlen to limit the end of arrays, and also masks the first
    and last 6 bp from each read since these are more prone to 
    alignmentn errors in denovo assemblies are will likely be 
    trimmed later.
    """
    # only use clusters with depth > min_depth_statistical for param estimates
    stat_mask, maxfrag = recal_hidepth_stat(data, sample)

    # sample many (e.g., 10_000) clusters to use for param estimation.
    maxclusts = min(size, stat_mask.sum())
    maxfrag = min(150, maxfrag)
    dims = (maxclusts, maxfrag, 4)
    stacked = np.zeros(dims, dtype=np.uint64)

    # mask restriction overhangs
    cutlens = [None, None]
    cutlens[0] = len(data.params.restriction_overhang[0])
    cutlens[1] = maxfrag - len(data.params.restriction_overhang[1])

    # fill stacked    
    clustgen = iter_clusters(sample.files.clusters, gzipped=True)
    for idx, clust in enumerate(clustgen):

        # skip masked (lowdepth) clusters
        if not stat_mask[idx]:
            continue

        # parse cluster and expand derep depths
        names = clust[0::2]
        seqs = clust[1::2]
        reps = [int(i.split("=")[-1][:-2]) for i in names]
        sseqs = [list(seq) for seq in seqs]
        arrayed = np.concatenate([
            [seq] * rep for seq, rep in zip(sseqs, reps)
        ])

        # select at most random 500 reads in a cluster
        if arrayed.shape[0] > 500:
            idxs = np.random.choice(
                range(arrayed.shape[0]), 
                size=500, 
                replace=False,
            )
            arrayed = arrayed[idxs]
                
        # trim edges for restriction lengths
        arrayed = arrayed[:, cutlens[0]:cutlens[1]]               
                
        # remove cols that are in or near pair separators or all Ns
        arrayed = arrayed[:, ~np.any(arrayed == "n", axis=0)]
        arrayed[arrayed == "-"] = "N"
        arrayed = arrayed[:, ~np.all(arrayed == "N", axis=0)]

        # store in stack
        catg = np.array([
            np.sum(arrayed == i, axis=0) for i in list("CATG")
            ],
            dtype=np.uint64
        ).T

        # Ensure catg honors the maxlen setting. If not you get a nasty
        # broadcast error.
        stacked[nclust, :catg.shape[0], :] = catg[:maxfrag, :]
        nclust += 1

        # maxclusters is enough, no need to do more.
        if done or (nclust == maxclusts):
            break

    # drop the empty rows in case there are fewer loci than the size of array
    newstack = stacked[stacked.sum(axis=2) > 0]
    assert not np.any(newstack.sum(axis=1) == 0), "no zero rows"
    return newstack



if __name__ == "__main__":

    import ipyrad as ip
    ip.set_log_level("DEBUG", log_file="/tmp/test.log")
   
    TEST = ip.load_json("/tmp/TEST5.json")
    TEST.run("4", force=True, quiet=False)
