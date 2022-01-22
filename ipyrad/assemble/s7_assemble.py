#!/usr/bin/env python

"""Filter loci and generate output files.

For reference-based assemblies it is optional to include the reference
sequence as a "Sample". If it is dropped then SNP calls are NOT MADE
relative to the reference -- output files that contain only SNPs will
include only sites that are variable among the real samples.
Nevertheless, the reference will appear as the REF allele in the VCF
file at these variable sites.

Format of .seqs.hdf5 database
-----------------------------
- 1-indexed scaff/locus ID
- 0-indexed 'phy' start position
- 0-indexed 'phy' end position
- ?-indexed reference start position
- ?-indexed reference end position

Format of 'snpsmap' dset in '.snps.hdf5' database
-------------------------------------------------
- 1-indexed locus ID                        # [1, 1, 2, 5, 5, ...]
- 0-indexed SNP index on locus              # [0, 1, 0, 0, 1, ...]
- 1-indexed SNP position on locus           # [10, 100, 25, 5, 15, ...]
- 0-indexed locus in this dataset counter   # [0, 0, 1, 2, 2, ...]
- 0-indexed SNP in this dataset counter     # [0, 1, 2, 3, 4, ...]
"""

import os
import glob
from collections import Counter
from pathlib import Path

from typing import TypeVar
from loguru import logger
import pandas as pd
import h5py

# internal imports
from ipyrad.core.schema import Project, AssemblyStats, Stats7
from ipyrad.core.progress_bar import AssemblyProgressBar
from ipyrad.assemble.base_step import BaseStep
from ipyrad.assemble.utils import IPyradError, clustdealer, splitalleles
from ipyrad.assemble.utils import GETCONS, DCONS, chroms2ints

# helper classes ported to separate files.
from ipyrad.assemble.write_outputs_processor import ChunkProcess
from ipyrad.assemble.write_outputs_converter import Converter
from ipyrad.assemble.write_outputs_to_loci import LociWriter
from ipyrad.assemble.write_outputs_to_seqs import SeqsDatabase
from ipyrad.assemble.write_outputs_to_snps import SnpsDatabase
# from ipyrad.assemble.write_outputs_vcf import FillVCF, build_vcf

# pylint: disable=too-many-branches, too-many-statements, too-many-lines

Assembly = TypeVar("Assembly")
logger = logger.bind(name="ipyrad")


class Step7(BaseStep):
    """Organization for step7 funcs.

    Users can branch assemblies between 6-7, but cannot merge.
    """
    def __init__(self, data: Assembly, force: bool, quiet: bool, ipyclient: "Client"):
        super().__init__(data, 7, quiet, force)

        self.data.tmpdir = self.tmpdir
        self.data.stepdir = self.stepdir
        self.ipyclient = ipyclient
        self.lbview = self.ipyclient.load_balanced_view()

        self.drop_ref = self.data.hackers.exclude_reference and self.data.is_ref
        """: record whether the reference sample needs to be dropped."""

        # store 'step samples' which is all samples that are ready for
        # this step in the current Assembly, plus the addition of a
        # 'reference' Sample if this is a reference assembly. These
        # samples must have the same 'database' file, meaning they 
        # were run in step6 together, checked in `check_database_files()`.
        # IF 'hackers.exclude_reference' it is excluded later.
        self.data.samples = self.samples
        logger.debug(f"samples: {sorted(self.data.samples)}")

        # info gathered from step6 results and ncpus
        self.chunksize = 0
        self.clust_database = ""
        self._check_database_files()
        self._get_chunksize()

        # default output file formats.
        self.data.outfiles = {
            'loci': Path(self.stepdir) / f"{self.data.name}.loci",
            'seqs_database': Path(self.stepdir) / f"{self.data.name}.seqs.hdf5",
            'snps_database': Path(self.stepdir) / f"{self.data.name}.snps.hdf5",
        }

        # create keys for additional file formats.
        for letter in self.data.params.output_formats:
            oname = OUT_SUFFIX[letter]
            self.data.outfiles[oname] = Path(self.stepdir) / f"{self.data.name}.{oname}"
            self.data.outfiles[oname].unlink(missing_ok=True)

        # init assembly stats object for storing results
        self.results = {}
        """: Store stats outputs from chunk processes."""
        self.data.assembly_stats = AssemblyStats()
        """: Store Assembly stats from chunk processes."""

    def _check_database_files(self):
        """Check that samples match those in the clustdb.

        All samples must have the same clust database file meaning
        that they were run together in step6. No merging of assemblies
        can occur between steps 6-7. This checks that the names in the
        database file are a superset of the names in self.samples
        """
        msg = (
            "It appears you likely merged assemblies between steps 6 "
            "and 7, which is not allowed. Samples must be merged into "
            "the same assembly before orthologs are identified in step 6. "
            "The simplest solution is to now re-run steps 6 on the merged "
            "assembly with the force=True flag before running step 7."
        )
        dbfiles = [
            sample.files.database for sname, sample in self.samples.items()
            if sname != "reference"
        ]
        if len(set(dbfiles)) > 1:
            raise IPyradError(
                f"Samples have different database files.\n{msg}")
        self.clust_database = dbfiles[0]

    def _get_chunksize(self):
        """Get nloci and ncpus to chunk and distribute jobs."""
        # this file is inherited from step 6 to allow step7 branching.
        with open(self.clust_database, 'r', encoding="utf-8") as inloci:
            # skip header
            inloci.readline()
            # get nraw loci
            nraws = sum(1 for i in inloci if i == "//\n") // 2

        # chunk to approximately 4 chunks per core
        ncpus = len(self.ipyclient.ids)
        self.chunksize = sum([
            (nraws // (ncpus * 4)),
            (nraws % (ncpus * 4)),
        ])
        logger.debug(f"using chunksize {self.chunksize} for {nraws} loci")

    def _split_clusters(self):
        """Splits the step6 clust_database into chunks to be processed
        in parallel by ChunkProcessor to apply filters.

        TODO: replace this with a generator function.
        """
        with open(self.clust_database, 'r', encoding="utf-8") as clusters:
            # skip header
            clusters.readline()

            # build iterator
            pairdealer = zip(*[clusters] * 2)

            # grab a chunk of clusters
            idx = 0
            while 1:

                # if an engine is available pull off a chunk
                try:
                    done, chunk = clustdealer(pairdealer, self.chunksize)
                except IndexError as err:
                    msg = f"clust_database formatting error in {chunk}"
                    logger.error(msg)
                    raise IPyradError from err

                # write to tmpdir and increment counter
                if chunk:
                    chunkpath = os.path.join(self.tmpdir, f"chunk-{idx}")
                    with open(chunkpath, 'w', encoding="utf-8") as outfile:
                        outfile.write("//\n//\n".join(chunk))
                    idx += 1

                # break on final chunk
                if done:
                    break
            logger.debug(f"split loci into {idx} chunks for processing.")

    def _apply_filters_and_trimming(self):
        """Calls process_chunk() function in parallel.

        The ChunkProcess class applies all of the step7 filters to
        raw loci, trims edges, and writes to tmpdir as .loci format.
        If `hackers.exclude_reference=True` it will filter the
        reference sequence from all loci
        """
        def remote_process_chunk(data, chunksize, chunkfile, drop_ref):
            """ChunkProcess writes to .loci chunks and returns stats."""
            proc = ChunkProcess(data, chunksize, chunkfile, drop_ref)
            proc.run()
            return proc.stats

        # organize chunks and submit to job engine
        chunks = glob.glob(os.path.join(self.tmpdir, "chunk-*"))
        chunks = sorted(chunks, key=lambda x: int(x.rsplit("-")[-1]))
        jobs = {}
        for chunk in chunks:
            args = (self.data, self.chunksize, chunk, self.drop_ref)
            jobs[chunk] = self.lbview.apply(remote_process_chunk, *args)
        msg = "applying filters"
        prog = AssemblyProgressBar(jobs, msg, 7, self.quiet)
        prog.block()
        prog.check()
        self.results = prog.results

    def _collect_stats(self):
        """Collect results from ChunkProcess and write stats file.

        Reads in stats data from each of the remote processed chunks,
        sums them, and writes to the JSON file in 'assembly_stats'.
        """
        # short name for entering stats
        stats = self.data.assembly_stats
        stats.nbases = 0
        stats.locus_cov = Counter({})
        stats.sample_cov = Counter({})
        stats.var_sites = Counter({})
        stats.pis_sites = Counter({})
        stats.var_props = Counter({})
        stats.pis_props = Counter({})

        # join dictionaries into global stats
        for chunkfile in self.results:
            data = self.results[chunkfile]
            stats.sample_cov.update(data['sample_cov'])
            stats.locus_cov.update(data['locus_cov'])
            stats.var_sites.update(data['var_sites'])
            stats.pis_sites.update(data['pis_sites'])
            stats.var_props.update(data['var_props'])
            stats.pis_props.update(data['pis_props'])
            stats.nbases += int(data['nbases'])
        del self.results

        # reorder site dicts
        stats.var_sites = {i: stats.var_sites[i] for i in sorted(stats.var_sites)}
        stats.pis_sites = {i: stats.pis_sites[i] for i in sorted(stats.pis_sites)}
        stats.var_props = {i: int(stats.var_props[i]) for i in sorted(stats.var_props)}
        stats.pis_props = {i: int(stats.pis_props[i]) for i in sorted(stats.pis_props)}

        # load all of the filters and concatenate
        filters = Path(self.data.tmpdir).glob("chunk-*.csv")
        filter_dfs = [pd.read_csv(str(i), index_col=0) for i in filters]
        filters = pd.concat(filter_dfs).sum(axis=0)

        # store stats to Project
        stats.nsamples = len(stats.sample_cov)
        stats.nloci = sum(stats.locus_cov.values())
        stats.nsnps = sum([i * stats.var_sites[i] for i in stats.var_sites])
            
        # store filter stats
        stats.filters.nloci_before_filtering = sum(stats.locus_cov.values()) + int(filters.sum())
        stats.filters.nloci_after_filtering = sum(stats.locus_cov.values())
        stats.filters.filtered_by_rm_duplicates = int(filters.dups)
        stats.filters.filtered_by_min_sample_cov = int(filters.minsamp)
        stats.filters.filtered_by_max_indels = int(filters.maxind)
        stats.filters.filtered_by_max_snps = int(filters.maxvar)
        stats.filters.filtered_by_max_shared_h = int(filters.maxshared)

        # RAISE EXCEPTION IF NO DATA PASSED FILTERING.
        if not self.data.assembly_stats.filters.nloci_after_filtering:
            raise IPyradError("no loci passed filtering")

        # store locus stats to Sample objects including the new 'reference' sample.
        for sname in self.data.samples:
            self.data.samples[sname].stats_s7 = Stats7(nloci=stats.sample_cov[sname])
            self.data.samples[sname].state = 7

        # write step7 json and report to logger
        self.data.save_json()
        logger.debug(
            "collecting statistics on assembly:\n"
            f"{self.data.assembly_stats.json(indent=2)}")

    def _write_stats_files(self):
        """Write the s7_stats file using results stored in JSON file.

        This is an easily human-readable summary of the stats of the
        assembly, made up of a subset of stats from the JSON file.
        """
        path = Path(self.data.stepdir) / "s7_assembly_stats.txt"
        with open(path, 'w', encoding="utf-8") as out:

            # load the filters assembly_stats as a dataframe.
            fdata = pd.DataFrame(
                index=list(self.data.assembly_stats.filters.dict()),
                columns=["filtered", "retained_loci"],
            )
            fdata.loc["nloci_before_filtering", "filtered"] = 0
            fdata.loc["nloci_before_filtering", "retained_loci"] = (
                self.data.assembly_stats.filters.nloci_before_filtering)
            fdata.loc["filtered_by_rm_duplicates", "filtered"] = (
                self.data.assembly_stats.filters.filtered_by_rm_duplicates)
            fdata.loc["filtered_by_rm_duplicates", "retained_loci"] = (
                fdata.iloc[0, 1] - sum(fdata.iloc[:2, 0]))
            fdata.loc["filtered_by_min_sample_cov", "filtered"] = (
                self.data.assembly_stats.filters.filtered_by_min_sample_cov)
            fdata.loc["filtered_by_min_sample_cov", "retained_loci"] = (
                fdata.iloc[0, 1] - sum(fdata.iloc[:3, 0]))
            fdata.loc["filtered_by_max_indels", "filtered"] = (
                self.data.assembly_stats.filters.filtered_by_max_indels)
            fdata.loc["filtered_by_max_indels", "retained_loci"] = (
                fdata.iloc[0, 1] - sum(fdata.iloc[:4, 0]))
            fdata.loc["filtered_by_max_snps", "filtered"] = (
                self.data.assembly_stats.filters.filtered_by_max_snps)
            fdata.loc["filtered_by_max_snps", "retained_loci"] = (
                fdata.iloc[0, 1] - sum(fdata.iloc[:5, 0]))
            fdata.loc["filtered_by_max_shared_h", "filtered"] = (
                self.data.assembly_stats.filters.filtered_by_max_shared_h)
            fdata.loc["filtered_by_max_shared_h", "retained_loci"] = (
                fdata.iloc[0, 1] - sum(fdata.iloc[:6, 0]))
            fdata.loc["nloci_after_filtering"] = (
                0, self.data.assembly_stats.filters.nloci_after_filtering)

            # write the header and then write the 'filters' dataframe.
            out.write(
                "# The number of loci before and after filtering. This table\n"
                "# shows the effects of filters applied during step 7.\n\n")
            fdata.to_string(buf=out)

            # write the header and then write the 'loci' dataframe.
            out.write(
                "\n\n# The number of loci recovered for each sample.\n\n")
            pd.DataFrame(
                index=list(self.data.assembly_stats.sample_cov),
                columns=["sample_coverage"],
                data=list(self.data.assembly_stats.sample_cov.values()),
            ).to_string(buf=out)

            # write the header and then write the 'sample' dataframe.
            out.write(
                "\n\n# The number of loci for which N taxa have data.\n"
                "# The 'reference' sample is included if present unless\n"
                "# using the 'hackers.exclude_reference=True' setting.\n\n")
            ldata = pd.DataFrame(
                index=list(self.data.assembly_stats.locus_cov),
                columns=["locus_coverage", "summed_locus_coverage"],
            )
            ldata.locus_coverage = list(self.data.assembly_stats.locus_cov.values())
            for i in ldata.index:
                ldata.loc[i, 'summed_locus_coverage'] = sum(ldata.locus_coverage[:i])
            ldata.to_string(buf=out)

            # write the header and then write the '% variation' dataframe.
            out.write(
                "\n\n# The distribution of % polymorphisms per locus.\n"
                "# This should be interpreted like a histogram.\n"
                "# nloci in each bin are shown using 0.1% intervals w/ addition of a small bin >0.\n"
                "# pis = parsimony informative variable sites (minor allele in >1 sample).\n"
                "# var = any variable site (pis + autapomorphies).\n\n")
            sdata = pd.DataFrame(
                index=list(self.data.assembly_stats.var_props),
                columns=["var_sites", "pis_sites"],
            )
            sdata.var_sites = list(self.data.assembly_stats.var_props.values())
            sdata.pis_sites = list(self.data.assembly_stats.pis_props.values())
            sdata.to_string(buf=out)

            # write the header and then write the 'N variation' dataframe.
            out.write(
                "\n\n# The distribution of N polymorphisms per locus.\n"
                "# This should be interpreted like a histogram.\n"                
                "# pis = only parsimony informative variable sites (minor allele in >1 sample).\n\n"
                "# var = any variable site (pis + autapomorphies).\n")
            maxval = max([
                max(self.data.assembly_stats.var_sites.keys()),
                max(self.data.assembly_stats.pis_sites.keys()),
            ])
            ssdata = pd.DataFrame(
                index=range(maxval + 1),
                columns=["var_sites", "pis_sites"],
            )
            ssdata.var_sites = [
                self.data.assembly_stats.var_sites[i]
                if i in self.data.assembly_stats.var_sites
                else 0
                for i in range(maxval + 1)
            ]
            ssdata.pis_sites = [
                self.data.assembly_stats.pis_sites[i]
                if i in self.data.assembly_stats.pis_sites
                else 0
                for i in range(maxval + 1)
            ]
            ssdata.to_string(buf=out)

            # write header and then write final summary of stats.
            out.write(
                "\n\n# Final sample stats summary\n"
                "# See JSON file or assembly subfolders for detailed "
                "stats on each assembly step.\n\n")
            self.data.stats.to_string(buf=out)

            # write alignment sizes 
            out.write(
                "\n\n# Alignment matrix statistics:\n"
                "# See ipyrad-analysis toolkit for tools to subsample loci, \n"
                "# SNPs, or matrices with options/filters for missing data "
                "# writing to files, and running analyses.\n\n")
            out.close()

    def _write_databases(self):
        """Write default formats: 'snps.hdf5', and 'seqs.hdf5'."""
        msg = "writing loci and database files"
        jobs = {0: self.lbview.apply(remote_fill_loci, self.data)}
        jobs = {1: self.lbview.apply(remote_fill_seqs, self.data)}
        # jobs = {2: self.lbview.apply(fill_snp_array, self.data)}
        prog = AssemblyProgressBar(jobs, msg, 7, self.quiet)
        prog.block()
        prog.check()

    def _write_conversions(self):
        """Writes data to optional output formats from HDF5 arrays."""
        msg = "writing conversions"
        jobs = {}
        for outf in self.outformats:
            jobs[outf] = self.lbview.apply(convert_outputs, *(self.data, outf))

        # iterate until all chunks are processed
        prog = AssemblyProgressBar(jobs, msg, 7, self.quiet)
        prog.block()
        prog.check()

        # store results to project
        for key in prog.results:
            outfile = prog.results[key]
            outname = OUT_SUFFIX[key]
            self.proj.outfiles[outname] = outfile
        print(self.proj.outfiles)

    def run(self):
        """All steps to complete step7 assembly."""
        # split clusters into bits given n engines.
        self._split_clusters()

        # apply filters and trim to the aligned clusters
        # and writes processed chunks to the tmpdir, and returns stats.
        self._apply_filters_and_trimming()
        self._collect_stats()
        self._write_stats_files()
        self._write_databases()
        # self._write_conversions()

        # send jobs to build vcf
        # throttle job to avoid memory errors based on catg size
        # if 'v' in self.formats:
            # self.remote_fill_depths()
            # self.remote_build_vcf()

    def _remote_build_vcf(self):
        """Build VCF format from snps HDF5, but w/o depths info."""
        printstr = "writing vcf output"
        prog = AssemblyProgressBar({}, None, printstr, self.data)
        prog.update()
        rasync = self.lbview.apply(build_vcf, self.data)
        prog.jobs = {0: rasync}
        prog.block()
        prog.check()

    # def remote_fill_depths(self):
    #     """
    #     Call fill_vcf_depths() in parallel.
    #     """
    #     printstr = ("indexing vcf depths ", "s7")
    #     prog = AssemblyProgressBar({}, None, printstr, self.data)
    #     prog.update()

    #     rasyncs = {}
    #     for sample in self.data.samples.values():
    #         if not sample.name == "reference":
    #             rasyncs[sample.name] = self.lbview.apply(
    #                 fill_vcf_depths, *(self.data, self.nsnps, sample))
    #     # iterate until all chunks are processed
    #     prog.jobs = rasyncs
    #     prog.block()
    #     prog.check()
###############################################################

def remote_fill_loci(data: Assembly) -> None:
    """Write .loci file from chunks."""
    LociWriter(data).run()

def remote_fill_seqs(data: Assembly) -> None:
    """Write .seqs.hdf5 file from chunks."""
    SeqsDatabase(data).run()

def remote_fill_snps(data: Assembly) -> None:
    """Write .snps.hdf5 file from chunks."""
    SnpsDatabase(data).run()

def remove_file_conversion(data: Assembly, outf: Path):
    """Remote function for converiting."""
    return Converter(data).run(outf)

def remote_write_vcf(data: Assembly) -> None:
    """..."""

def remove_fill_vcf_depths(data, nsnps, sample):
    """Writes catg depths to a tmp HDF5 for this sample."""
    filler = FillVCF(data, nsnps, sample)
    filler.run()

    # write vcfd to file and cleanup
    vcfout = os.path.join(data.tmpdir, sample.name + ".depths.hdf5")
    with h5py.File(vcfout, 'w') as io5:
        io5.create_dataset(
            name="depths",
            data=filler.vcfd,
            )
    del filler


# ------------------------------------------------------------
# funcs parallelized on remote engines
# -------------------------------------------------------------
# def write_loci(data):
#     """Write the .loci file from processed loci chunks.
    
#     Concatenates .loci chunk files into a single file.

#     Notes
#     -----
#     Writing of the .alleles format is currently deprecated (v.1.0)
#     """
#     # parse `reference.fai` to get dict of enumerated {int: scaffname}
#     if data.is_ref:
#         faidict = chroms2ints(data, True)

#     # gather all processed .loci bits and order them.
#     locibits = glob.glob(os.path.join(data.tmpdir, "*.loci"))
#     sortbits = sorted(locibits,
#         key=lambda x: int(x.rsplit("-", 1)[-1][:-5]))

#     # write to file while adding counters to the ordered loci
#     outfile = os.path.join(data.stepdir, f"{data.name}.loci")
#     outloci = open(outfile, 'w', encoding="utf-8")

#     idx = 0
#     for bit in sortbits:
#         # store until writing
#         lchunk = []

#         # LOCI ONLY: iterate through chunk files
#         indata = open(bit, 'r', encoding="utf-8")
#         for line in iter(indata):

#             # write name, seq pairs
#             if "|\n" not in line:
#                 lchunk.append(line)  # [:5] + line[5:].upper())

#             # write snpstring and info
#             else:
#                 snpstring, nidxs = line.rsplit("|", 2)[:2]
#                 if data.is_ref:
#                     refpos = nidxs.split(",")[0]

#                     # translate refpos chrom idx (1-indexed) to chrom name
#                     cid, rid = refpos.split(":")
#                     cid = faidict[int(cid) - 1]
#                     lchunk.append(
#                         "{}|{}:{}:{}|\n".format(snpstring, idx, cid, rid))
#                 else:
#                     lchunk.append(
#                         "{}|{}|\n".format(snpstring, idx))
#                 idx += 1
#         # close bit handle
#         indata.close()
#         outloci.write("".join(lchunk))
#     outloci.close()


BADPOP_SAMPLES = """
There are sample names in the populations assignments that are not present in
this assembly. This is likely due to a typo and should be corrected. The
following sample names are in the pop assignments but not in this Assembly:
{}
"""
POPULATION_REQUIRED = """\
Warning: Skipping output format '{}'. Requires population assignments.
You can alternatively create this type of file using ipyrad-analysis
after assembling your data.
"""

OUT_SUFFIX = {
    'l': 'loci',
    'p': 'phy',
    's': 'snps',  # 'snpsmap'
    'n': 'nex',
    'k': 'str',
    'g': 'geno',
    'G': 'gphocs',
    'u': 'usnps',  # 'ustr', 'ugeno',
    'v': 'vcf',
    't': 'treemix',
    'm': 'migrate',
    # 'a' ('alleles',),
}


if __name__ == "__main__":

    import ipyrad as ip
    ip.set_log_level("DEBUG", log_file="/tmp/test.log")


    TEST = ip.load_json("/tmp/TEST5.json")
    PROJ = Project.parse_file(TEST.json_file)
    TEST.tmpdir = "/tmp/TEST5_tmp_outfiles"
    TEST.stepdir = "/tmp/TEST5_outfiles"
    # fill_seq_array(TEST, PROJ)

    TEST.run("7", force=True, quiet=True)

    # tdata = ip.load_json("/tmp/test-simpairddrad.json")
    # tdata.params.output_formats = "lpsnkaguvtm"
    # tdata.run("7", auto=True, force=True)
    # logger.info(tdata.stats.T)

    # tdata = ip.load_json("/tmp/test-amaranth.json")
    # tdata.run("7", auto=True, force=True)
    # print(tdata.stats)
    # print(tdata.stats_dfs.s5)

    # self.data.hackersonly.declone_PCR_duplicates:
    # tdata = ip.load_json("/tmp/test-amaranth-denovo.json")
    # tdata.ipcluster['cores'] = 4
    # tdata.run("7", auto=True, force=True)
    # logger.info(tdata.stats.T)


    ## compare .loci file to extracted loci from HDF5.
    # with h5py.File("./refdata_outfiles/refdata.seqs.hdf5", "r") as io5:
    #     names = (io5["phymap"].attrs['phynames'])
    #     scaffidx, start, end, pos0, pos1 = io5["phymap"][2]
    #     print(scaffidx, start, end, pos0, pos1)
    #     arr = io5["phy"][:, start:end]
    #     for idx in range(arr.shape[0]):
    #         print(
    #           f"{names[idx]}\t",
    #           b"".join(arr[idx, :10].view("S1")).decode(),
    #           b"".join(arr[idx, -10:].view("S1")).decode())