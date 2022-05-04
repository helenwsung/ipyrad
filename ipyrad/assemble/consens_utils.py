#!/usr/bin/env python

"""Utilities for s5_consensus

Alignment files have been broken into chunked files with X clusters in 
each, containing only high depth clusters. This iterates through the
clusters to apply site and locus/allele filtering.

HDF5 storage
------------
cats: shape=(nclusters, max_frag, 4), dtype=np.uint16
    Site depths at every base call in order "CATG".
alls: ...
    Allele counts at each locus. This could be used for post-filtering
    in step7, but is not currently used. It has been deprecated, and
    is not no longer stored, but the code was left in place in case
    we change our minds.
chroms: ...
    The scaffold (index, start, end) for every cluster.
"""

from typing import TypeVar, Iterator, List, Tuple, Dict
import sys
import gzip
from pathlib import Path
import subprocess as sps
from collections import Counter
import h5py
import numpy as np
import pandas as pd
import scipy.stats
from ipyrad.assemble.clustmap_within_denovo_utils import iter_clusters
from ipyrad.assemble.utils import IPyradError, DCONS, TRANS, CIGARDICT

Assembly = TypeVar("Assembly")
Sample = TypeVar("Sample")
BIN_SAMTOOLS = Path(sys.prefix) / "bin" / "samtools"


class Locus:
    def __init__(self, names: List[str], seqs: np.ndarray, refpos: Tuple[int,int,int]):
        self.names = names
        self.seqs = seqs
        self.refpos = refpos
        self.consens = None
        self.hidx = None
        self.nheteros = None
        self.nalleles = 1


class Processor:
    """Consensus base and allele calling.

    The consensus calling process that calls alleles and genotypes and
    applies filters to consens seqs. It writes results to two files
    for each chunk, tmpcons (SAM) and tmpcats (HDF5).
    """
    def __init__(self, data: Assembly, sample: Sample, chunkfile: Path):

        self.data = data
        """: Assembly object with param settings."""
        self.sample = sample
        """: Sample object with file paths and stats."""        
        self.chunkfile = chunkfile
        """: Path of tmp file with chunk of alignments from Sample."""                

        # set max limits and params from data
        self.maxlen = self.data.max_frag
        """: Max allowed length of a consensus locus."""
        self.maxhet = self.data.params.max_h_consens
        """: Max allowed heterozygote (proportion) in consens locus."""
        self.maxalleles = self.data.params.max_alleles_consens
        """: Max allowed alleles identified in consens locus."""
        self.maxn = self.data.params.max_n_consens if not self.data.is_ref else int(1e6)
        """: Max allowed proportion of Ns in a consens locus."""

        # these will be filled and then dumped to out files.
        self.nalleles: List[int] = []
        """: Number of alleles found in each consensus locus."""
        self.refpos: List[Tuple[int,int,int]] = []
        """: Tuple of scaffid, start, end positions (0-indexed)."""
        self.consens_seqs: List[str] = []
        """: List of consensus sequences with ambiguity codes."""
        self.catgs: List[np.ndarray] = []
        """: List of arrays with base depths."""

        # stats dicts will be returned by the remote process
        self.counters = {
            "chunk_start": int(self.chunkfile.name.split("_")[-1]),
            "nheteros": 0,
            "nsites": 0,
            "nconsens": 0,
        }
        self.filters = {
            "depth": 0,
            "maxh": 0,
            "maxn": 0,
            "maxalleles": 0,
        }

        # store data for writing
        self.faidict: Dict[int: str] = {}
        self.revdict: Dict[str: int] = {}
        self.get_reference_fai()

    def get_reference_fai(self):
        """Parse samtools fai to get reference scaff index to name map.

        If reference-mapped then parse the fai (TSV) to get all scaffs.
        Names and order of scaffs is not predictable so we create a
        dict to map {int: scaffname} and {scaffname: int}
        """
        if self.data.is_ref:
            fai = pd.read_csv(
                self.data.params.reference_sequence + ".fai",
                names=['scaffold', 'size', 'sumsize', 'a', 'b'],
                sep="\t",
                dtype=object)
            self.faidict = {j: i for i, j in enumerate(fai.scaffold)}
            self.revdict = {j: i for i, j in self.faidict.items()}

    def run(self):
        """The main function to run the processor"""
        self.collect_data()
        self.write_chunk()

    def iter_process_chunks(self) -> Iterator:
        """Iterates over clusters to apply filters."""
        for clust in iter_clusters(self.chunkfile):

            # parse names and sequences from cluster
            names = clust[::2]
            seqs = clust[1::2]
            reps = [int(i.split(";")[-2][5:]) for i in names]
            sarr = np.zeros((sum(reps), len(seqs[0].strip())), dtype=np.uint8)

            # expand the dereplicated sequences into a large array
            idx = 0
            for ireps, iseq in zip(reps, seqs):
                iseq = iseq.strip()
                for _ in range(ireps):
                    sarr[idx] = np.array(list(iseq)).astype(bytes).view(np.uint8)
                    idx += 1

            # trim ncols to allow maximum length
            seqs = sarr[:, :self.maxlen]

            # trim nrows to allow maximum depth (65_535 is np.uint16 limit)
            seqs = sarr[:65_500, :self.maxlen]

            # ref positions (chromint, pos_start, pos_end)
            ref_position = (-1, 0, 0)
            if self.data.is_ref:
                # parse position from name string
                rname = names[0].rsplit(";", 2)[0]
                chrom, posish = rname.rsplit(":")
                pos0, pos1 = posish.split("-")
                # drop `tag=ACGTACGT` if 3rad and declone_PCR_duplicates is set
                pos1 = pos1.split(";")[0]
                # pull idx from .fai reference dict
                chromint = self.faidict[chrom] + 1
                ref_position = (int(chromint), int(pos0), int(pos1))

            # yield result as a Locus
            loc = Locus(names=names, seqs=sarr, refpos=ref_position)
            yield loc

    def iter_mask_repeats(self) -> Iterator:
        """denovo only: mask repeats, returns True if clust is filtered."""
        for loc in self.iter_process_chunks():

            # if reference assembly then no filter applies (return False)
            if self.data.is_ref:
                yield loc

            else:
                # get column counts of dashes
                idepths = np.sum(loc.seqs == 45, axis=0).astype(float)

                # get proportion of bases that are dashes at each site
                props = idepths / loc.seqs.shape[0]

                # if prop dash sites is more than 0.9 drop as bad align
                keep = props < 0.9
                loc.seqs = loc.seqs[:, keep]
                loc.seqs[loc.seqs == 45] = 78

                # if 'n' spacer is in any site then convert col to 'n'
                loc.seqs[:, np.any(loc.seqs == 110, axis=0)] = 110

                # apply filter in case ALL sites were dropped
                if loc.seqs.size:
                    yield loc
                else:
                    self.filters['depth'] += 1

    def iter_filter_mindepth(self) -> Iterator:
        """filter for mindepth: returns True if clust is filtered."""
        for loc in self.iter_mask_repeats():
            sumreps = loc.seqs.shape[0]
            bool1 = sumreps >= self.data.params.min_depth_majrule
            bool2 = sumreps <= self.data.params.max_depth
            if bool1 & bool2:
                yield loc
            else:
                self.filters['depth'] += 1

    def iter_build_consens(self) -> Iterator:
        """Filter..."""
        for loc in self.iter_filter_mindepth():

            # get unphased genotype call of this sequence
            loc.consens, loc.triallele = new_base_caller(
                loc.seqs,
                self.data.params.min_depth_majrule,
                self.data.params.min_depth_statistical,
                self.data.hetero_est,
                self.data.error_est,
            )

            # TODO: for denovo we could consider trimming Ns from internal
            # split near pair separator, applied here.
            # logger.debug(consens)

            # trim Ns (uncalled genos) from the left and right
            trim = np.where(loc.consens != 78)[0]

            # if everything was trimmed then return empty
            if not trim.any():
                self.filters["mindepth"] += 1
                continue

            if loc.triallele and self.data.params.max_alleles_consens < 3:
                self.filters['maxalleles'] += 1
                continue

            # otherwise trim edges
            ltrim = trim.min()
            rtrim = trim.max() + 1
            loc.consens = loc.consens[ltrim: rtrim]
            loc.seqs = loc.seqs[:, ltrim: rtrim]

            # update position for trimming
            loc.refpos = (
                loc.refpos[0],
                loc.refpos[1] + ltrim,
                loc.refpos[1] + ltrim + rtrim,
            )

            # return triallele filter, mindepth2 filter
            yield loc

    def iter_filter_heteros(self) -> Iterator:
        """..."""
        for loc in self.iter_build_consens():
            hsites = np.any([
                loc.consens == 82,
                loc.consens == 75,
                loc.consens == 83,
                loc.consens == 89,
                loc.consens == 87,
                loc.consens == 77,
            ], axis=0)

            loc.hidx = np.where(hsites)[0]
            loc.nheteros = loc.hidx.size

            # filters
            if loc.nheteros > (loc.consens.size * self.maxhet):
                self.filters['maxh'] += 1
            elif loc.consens.size < self.data.params.filter_min_trim_len:
                self.filters['maxn'] += 1
            elif (loc.consens == 78).sum() > (loc.consens.size * self.maxn):
                self.filters['maxn'] += 1
            else:
                yield loc

    def iter_filter_alleles(self) -> Iterator:
        """Infer the number of alleles from haplotypes.

        Easy case
        ---------
        >>> AAAAAAAAAATAAAAAAAAAACAAAAAAAA
        >>> AAAAAAAAAACAAAAAAAAAATAAAAAAAA

        T,C
        C,T

        Challenging case (more likely paralogs slip by)
        -----------------------------------------------
        >>> AAAAAAAAAATAAAAAAAA-----------
        >>> AAAAAAAAAACAAAAAAAA-----------
        >>> -------------AAAAAAAACAAAAAAAA
        >>> -------------AAAAAAAATAAAAAAAA

        T -
        C -
        - C
        - T
        """
        for loc in self.iter_filter_heteros():

            # if only one hetero site then there are only two bi-alleles
            if loc.nheteros == 1:
                loc.nalleles = 2
                yield loc
                continue

            # array of hetero sites (denovo: this can include Ns)
            harray = loc.seqs[:, loc.hidx]

            # remove varsites containing N
            harray = harray[~np.any(harray == 78, axis=1)]

            # remove alleles containing a site not called in bi-allele genos
            calls0 = np.array([DCONS[i][0] for i in loc.consens[loc.hidx]])
            calls1 = np.array([DCONS[i][1] for i in loc.consens[loc.hidx]])
            bicalls = (harray == calls0) | (harray == calls1)
            harray = harray[np.all(bicalls, axis=1), :]

            # get counts of each allele (e.g., AT:2, CG:2)
            ccx = Counter([tuple(i) for i in harray])

            # PASSED PARALOG FILTERING
            # there are fewer alleles than the limit (default=2)
            if len(ccx) <= self.maxalleles:
                loc.nalleles = len(ccx)
                yield loc
                continue

            # MAYBE CAN PASS ---------------------------------------
            # below here tries to correct alleles in case of errors
            # ------------------------------------------------------

            # Try dropping alleles with very low coverage (< 10%)
            alleles = []
            for allele in ccx:
                if (ccx[allele] / loc.seqs.shape[0]) >= 0.1:
                    alleles.append(allele)  # ccx[allele]

            # in case an allele was dropped, now check again.
            # apply filter if all were dropped as lowcov
            ccx = Counter(alleles)
            if len(ccx) <= self.maxalleles:
                loc.nalleles = len(ccx)
                yield loc
                continue

            # DID NOT PASS FILTER ----------------------------------
            self.filters['maxalleles'] += 1
            loc.nalleles = len(ccx)

    def collect_data(self):
        """Store the site count data for writing to HDF5, and stats.
        
        Iterates through all loci that passed filtering, and computed
        filter stats on the way, and stores the stats to the Processor
        and collects the Locus data into arrays for storing in HDF5.
        The temp arrays will be concatenated back on the main processor
        instead of on the remote engine.
        """
        for loc in self.iter_filter_alleles():

            # advance counters
            # self.counters["chunk_start"] += 1            
            self.counters["nconsens"] += 1
            self.counters["nheteros"] += loc.nheteros

            # store nsites w/o counting missing or spacer sites.
            self.counters["nsites"] += loc.consens.size
            self.counters["nsites"] -= sum(loc.consens == 78)
            self.counters["nsites"] -= sum(loc.consens == 110)            

            # store the sequence until writing
            self.consens_seqs.append(bytes(loc.consens).decode())
            self.nalleles.append(loc.nalleles)
            self.refpos.append(loc.refpos)

            # store a reduced array with only CATG
            catg = [np.sum(loc.seqs == i, axis=0) for i in (67, 65, 84, 71)]
            catg = np.array(catg, dtype=np.uint16).T
            self.catgs.append(catg)
            # [cidx, :catg.shape[0], :] = catg

    def write_chunk(self):
        """Write chunk of consens reads to disk, store data and stats.

        depths, alleles, and chroms. For denovo data writes consens
        chunk as a fasta file. For reference data it writes as a 
        SAM file that is compatible to be converted to BAM (very 
        stringent about cigars).
        """
        # enter catgs data into an array
        catgs = np.zeros((len(self.catgs), self.maxlen, 4), dtype=np.uint16)
        for idx, arr in enumerate(self.catgs):
            subset = arr[:self.maxlen]
            catgs[idx, :subset.shape[0]] = subset

        # store the database data
        with h5py.File(self.chunkfile.with_suffix(".tmpcatgs.hdf5"), 'w') as io5:
            io5.create_dataset(name="catgs", data=catgs, dtype=np.uint16)
            io5.create_dataset(name="nalleles", data=np.array(self.nalleles), dtype=np.uint8)
            io5.create_dataset(name="refpos", data=np.array(self.refpos), dtype=np.int64)
        del self.catgs
        del self.nalleles
        del self.refpos

        # write final consens string chunk
        handle = self.chunkfile.with_suffix(".tmpconsens")

        # write consens data to the consens file
        if not self.data.is_ref:    
            with open(handle, 'w', encoding="utf-8") as out:
                out.write("\n".join(self.consens_seqs))

        # reference needs to store if the read is revcomp to reference
        else:
            with open(handle, 'w', encoding="utf-8") as out:            
                data = [

                ]
                # TODO -----------------------------------------
                constring = "\n".join(
                    ["{}:{}:{}-{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}"
                    .format(
                        self.sample.name,
                        self.refarr[i][0],
                        self.refarr[i][1],
                        self.refarr[i][1] + len(self.storeseq[i]),
                        #self.refarr[i][2],
                        0,
                        self.revdict[self.refarr[i][0] - 1],
                        self.refarr[i][1],
                        0,
                        "{}M".format(len(self.storeseq[i])),
                        "*",
                        0,
                        #self.refarr[i][2] - self.refarr[i][1],
                        len(self.storeseq[i]),
                        self.storeseq[i],
                        "*",
                    ) for i in self.storeseq]
                )


def new_base_caller(sarr, mindepth_mj, mindepth_st, est_het, est_err):
    """Call site genotypes from site counts. Uses scipy binomial.
    """
    # start with an array of Ns
    cons = np.zeros(sarr.shape[1], dtype=np.uint8)
    cons.fill(78)

    # record if evidence of a tri-allele
    triallele = 0

    # iterate over columns
    for cidx in range(sarr.shape[1]):

        # get col, nmask, dashmask, and bothmask
        col = sarr[:, cidx]
        nmask = col == 78
        dmask = col == 45
        bmask = nmask | dmask

        # if non-masked site depth is below mindepth majrule fill with N (78)
        if np.sum(~bmask) < mindepth_mj:
            cons[cidx] = 78
            continue

        # if not variable (this will include the 'nnnn' pair separator (110))
        # in denovo data sets
        masked = col[~bmask]
        if np.all(masked == masked[0]):
            cons[cidx] = masked[0]
            continue

        # make statistical base calls on allele frequencies
        counts = np.bincount(col, minlength=79)
        counts[78] = 0
        counts[45] = 0

        # get allele freqs (first-most, second, third = p, q, r)
        pbase = np.argmax(counts)
        nump = counts[pbase]
        counts[pbase] = 0

        qbase = np.argmax(counts)
        numq = counts[qbase]
        counts[qbase] = 0

        rbase = np.argmax(counts)
        numr = counts[rbase]
        counts[rbase] = 0

        # if third allele occurs >X% then fill N and mark as paralog
        # 1/6 as the cutoff
        if (numr / (nump + numq + numr)) >= 0.15:
            triallele = 1

        # based on biallelic depth
        bidepth = nump + numq
        if bidepth < mindepth_mj:
            cons[cidx] = 78
            continue

        # if depth is too high, reduce to sampled int to avoid overflow
        if bidepth > 500:
            nump = int(500 * (nump / float(bidepth)))
            numq = int(500 * (numq / float(bidepth)))

        # make majority-rule call
        if bidepth < mindepth_st:
            if nump == numq:
                cons[cidx] = TRANS[(pbase, qbase)]
            else:
                cons[cidx] = pbase

        # make statistical base call
        ishet, prob = get_binom(nump, numq, est_err, est_het)
        if prob < 0.95:
            cons[cidx] = 78
        else:
            if ishet:
                cons[cidx] = TRANS[(pbase, qbase)]
                # print(f'cidx {cidx}; cons[cidx] {cons[cidx]}')
            else:
                cons[cidx] = pbase
    return cons, triallele


def get_binom(base1, base2, est_err, est_het):
    """Return binomial probability of base call."""
    prior_homo = (1. - est_het) / 2.
    prior_hete = est_het

    ## calculate probs
    bsum = base1 + base2
    hetprob = scipy.special.comb(bsum, base1) / (2. ** (bsum))
    homoa = scipy.stats.binom.pmf(base2, bsum, est_err)
    homob = scipy.stats.binom.pmf(base1, bsum, est_err)

    ## calculate probs
    hetprob *= prior_hete
    homoa *= prior_homo
    homob *= prior_homo

    ## final
    probabilities = [homoa, homob, hetprob]
    bestprob = max(probabilities) / float(sum(probabilities))

    ## return
    if hetprob > homoa:
        return True, bestprob
    return False, bestprob


def make_cigar(arr):
    """Writes a cigar string with locations of indels and lower case ambigs."""
    # simplify data
    arr[np.char.islower(arr)] = '.'
    indel = np.bool_(arr == "-")
    ambig = np.bool_(arr == ".")
    arr[~(indel + ambig)] = "A"

    # counters
    cigar = ""
    mcount = 0
    tcount = 0
    lastbit = arr[0]
    for _, j in enumerate(arr):

        # write to cigarstring when state change
        if j != lastbit:
            if mcount:
                cigar += "{}{}".format(mcount, "M")
                mcount = 0
            else:
                cigar += "{}{}".format(tcount, CIGARDICT.get(lastbit))
                tcount = 0
            mcount = 0

        # increase counters
        if j in ('.', '-'):
            tcount += 1
        else:
            mcount += 1
        lastbit = j

    # write final block
    if mcount:
        cigar += "{}{}".format(mcount, "M")
    if tcount:
        cigar += '{}{}'.format(tcount, CIGARDICT.get(lastbit))
    return cigar


def concat_catgs(data: Assembly, sample: Sample) -> None:
    """Concat catgs into a single sample catg and remove tmp files

    The catg hdf5 array stores the depths of all bases at each site
    that was retained in a consensus allele. These will be used later
    in the VCF output file to print depths and base call scores.
    """
    # collect tmpcat files written by write_chunks()
    tmpcats = list(data.tmpdir.glob(f"{sample.name}_chunk_[0-9]*.tmpcatgs.hdf5"))
    tmpcats.sort(key=lambda x: int(x.name.split("_")[-2]))

    # dimensions of final h5
    optim = 5_000  # chunksize for low-mem fetching later
    nrows = sample.stats_s5.consensus_total
    maxlen = data.max_frag

    # fill in the chunk array
    with h5py.File(sample.files.depths, 'w') as ioh5:
        
        h5_catgs = ioh5.create_dataset(
            name="catgs",
            shape=(nrows, maxlen, 4),
            dtype=np.uint16,
            chunks=(optim, maxlen, 4),
            compression="gzip")
        h5_nalleles = ioh5.create_dataset(
            name="nalleles",
            shape=(nrows, ),
            dtype=np.uint8,
            chunks=(optim, ),
            compression="gzip")

        # only create chrom for reference-aligned data
        if data.is_ref:
            h5_refpos = ioh5.create_dataset(
                name="refpos",
                shape=(nrows, 3),
                dtype=np.int64,
                chunks=(optim, 3),
                compression="gzip")

        # Combine all those tmp cats into the big cat
        start = 0
        for icat in tmpcats:
            with h5py.File(icat, 'r') as io5:
                end = start + io5['catgs'].shape[0]
                h5_catgs[start:end] = io5['catgs'][:]
                h5_nalleles[start:end] = io5['nalleles'][:]
                if data.is_ref:
                    h5_refpos[start:end] = io5['refpos'][:]
            start = end
            icat.unlink()

def concat_denovo_consens(data: Assembly, sample: Sample) -> None:
    """Concatenate consens bits into fasta file for denovo assemblies.
    
    Writes to fasta-like format. For denovo pair data it also checks 
    that there exists data on both sides of the nnnn separator, and 
    if not it adds an N block, to provide a minimum length for muscle
    alignment.

    Example
    -------
    >name_0
    AAAAAAAAAAAAAAAAAAAAAA
    >name_1
    TTTTTTTTTTTTTTTTTTTTTT

    >name_0                    ->  >name_0
    AAAAAAAAAnnnnTTTTTTTTT     ->  AAAAAAAAAAnnnnTTTTTTTTTT
    >name_1                    ->  >name_1
    nnnnTTTTTTTT               ->  NNNNNNNNNNnnnnTTTTTTTTTT

    """
    # collect consens chunk files
    combs1 = list(data.tmpdir.glob(f"{sample.name}_chunk_[0-9]*.tmpconsens"))
    combs1.sort(key=lambda x: int(x.name.split("_")[-2]))

    # stream through file renaming consens reads and write out
    idx = 0
    with gzip.open(sample.files.consens, 'wt', encoding="utf-8") as out:
        for fname in combs1:
            cstack = []
            with open(fname, 'r', encoding="utf-8") as infile:
                for line in infile:
                    # ensure data exists on both sides of the nnnn
                    if "nnnn" in line:
                        try:
                            before, after = line.split("nnnn")
                            if len(before) < 15:
                                before = ("N" * 15) + before
                            if len(after) < 15:
                                after = ("N" * 15) + after
                            line = f"{before}nnnn{after}"
                        except (ValueError, IndexError):
                            print(f"{fname} ----\n\n{line}\n\n")
                            continue

                    cstack.append(f">{sample.name}_{idx}\n{line}")
                    idx += 1
                out.write("".join(cstack) + "\n")
            #fname.unlink()

def concat_reference_consens(data, sample):
    """
    Concatenates consens bits into SAM for reference assemblies
    """
    # write sequences to SAM file
    chunks = list(data.tmpdir.glob(f"{sample.name}_[0-9]*.tmpconsens"))    
    chunks.sort(key=lambda x: int(x.split(".")[-1]))

    # open sam handle for writing to bam
    bamfile = data.stepdir / f"{sample.name}.bam"
    samfile = data.tmpdir / f"{sample.name}.sam"
    with open(samfile, 'w', encoding="utf-8") as outf:

        # parse fai file for writing headers
        fai = "{}.fai".format(data.params.reference_sequence)
        fad = pd.read_csv(fai, sep="\t", names=["SN", "LN", "POS", "N1", "N2"])
        headers = ["@HD\tVN:1.0\tSO:coordinate"]
        headers += [
            "@SQ\tSN:{}\tLN:{}".format(i, j)
            for (i, j) in zip(fad["SN"], fad["LN"])
        ]
        outf.write("\n".join(headers) + "\n")

        # write to file with sample names imputed to line up with catg array
        counter = 0
        for fname in chunks:
            with open(fname) as infile:
                # impute catg ordered seqnames
                data = infile.readlines()
                fdata = []
                for line in data:
                    name, chrom, rest = line.rsplit(":", 2)
                    fdata.append(
                        "{}_{}:{}:{}".format(name, counter, chrom, rest)
                        )
                    counter += 1
                outf.write("".join(fdata) + "\n")

    # convert to bam
    cmd = [BIN_SAMTOOLS, "view", "-Sb", samfile]
    with open(bamfile, 'w') as outbam:
        proc = sps.Popen(cmd, stderr=sps.PIPE, stdout=outbam)
        comm = proc.communicate()[1]
    if proc.returncode:
        raise IPyradError("error in samtools: {}".format(comm))



# def encode_alleles(consens, hidx, alleles):
#     """
#     Store phased allele data for diploids
#     """
#     ## find the first hetero site and choose the priority base
#     ## example, if W: then priority base in A and not T. PRIORITY=(order: CATG)
#     bigbase = PRIORITY[consens[hidx[0]]]

#     ## find which allele has priority based on bigbase
#     bigallele = [i for i in alleles if i[0] == bigbase][0]

#     ## uplow other bases relative to this one and the priority list
#     ## e.g., if there are two hetero sites (WY) and the two alleles are
#     ## AT and TC, then since bigbase of (W) is A second hetero site should
#     ## be stored as y, since the ordering is swapped in this case; the priority
#     ## base (C versus T) is C, but C goes with the minor base at h site 1.
#     #consens = list(consens)
#     for hsite, pbase in zip(hidx[1:], bigallele[1:]):
#         if PRIORITY[consens[hsite]] != pbase:
#             consens[hsite] = consens[hsite].lower()

#     ## return consens
#     return consens

