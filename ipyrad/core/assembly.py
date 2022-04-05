#!/usr/bin/env python

"""Assembly class is the core object for calling assembly steps.


"""

from typing import List, Optional, Dict, Tuple
import shutil
# import asyncio
# from pathlib import Path

from loguru import logger
import pandas as pd
from ipyrad.core.params_schema import ParamsSchema, HackersSchema
from ipyrad.core.schema import Project, SampleSchema
from ipyrad.core.cluster import Cluster
from ipyrad.assemble.utils import IPyradError
from ipyrad.assemble.s1_demux import Step1
from ipyrad.assemble.s2_trim_reads import Step2
from ipyrad.assemble.s3_clustmap_within import Step3
from ipyrad.assemble.s4_joint_estimate import Step4
from ipyrad.assemble.s5_consensus import Step5
from ipyrad.assemble.s6_clustmap_across import Step6
from ipyrad.assemble.s7_assemble import Step7

# pylint: disable=too-many-branches
logger = logger.bind(name="ipyrad")


class Assembly:
    """Assembly class for storing params, connecting to cluster, and
    running assembly steps.

    Notes
    -----
    The Assembly object can be serialzed to JSON using .save_json(), 
    and reloaded from JSON using .load_json(). The schema for 
    converting back and forth between object and JSON checks the 
    types of the Params.
    
    Examples
    --------
    >>> import ipyrad as ip
    >>> data = ip.Assembly(name="test")
    >>> data.params.sorted_fastq_path = "..."
    >>> data.run("1")
    """
    def __init__(self, name: str):
        # core JSON file components
        self.params = ParamsSchema(assembly_name=name)
        """: A class storing assembly parameters."""
        self.hackers = HackersSchema()
        """: A class storing 'Hackers' parameters that are for power-users."""
        self.samples: Dict[str, SampleSchema] = {}
        """: A dictionary mapping sample names to Sample objects."""
        self.populations: Dict[str, Tuple] = {}
        """: A optional dict of {popnames: ([samples], min)}"""
        self.outfiles: Dict[str, str] = {}
        """: A dict with step7 output file paths, if assembly finished."""
        self.assembly_stats: Dict[str, pd.DataFrame] = {}
        """: A dict-like class with *final stats* for completed assembly."""
        self.ipcluster = {'cores': None, 'threads': 2}
        """: Dict with default parallization args."""

    def __repr__(self):
        return f"<ipyrad.Assembly object {self.name}>"

    @property
    def name(self) -> str:
        """Return the assembly_name from Assembly.params"""
        return self.params.assembly_name

    @property
    def json_file(self) -> str:
        """Return JSON file as {project_dir}/{assembly_name}.json"""
        return self.params.project_dir / (self.params.assembly_name + ".json")

    @property
    def is_ref(self) -> bool:
        """Return whether Assembly is reference method, based on params."""
        return self.params.assembly_method == "reference"

    @property
    def is_pair(self) -> bool:
        """Returns whether Assembly is paired datatype, based on params."""
        return "pair" in self.params.datatype

    @property
    def stats(self) -> pd.DataFrame:
        """Return a dataframe with *summary stats* extracted from JSON.

        Returns stats given the current completed assembly steps.
        To see more detailed stats from specific steps see instead
        the self.stats_dfs.
        """
        # using self object (any cases where we need to load from json?)
        # create dataframe with only the summary stats as columns
        stats = pd.DataFrame(
            index=sorted(self.samples),
            columns=[
                'state',
                'reads_raw',
                'reads_passed_filter',
                'clusters_total',
                'clusters_hidepth',
                'mean_depth_total',
                'reads_mapped_to_ref_prop',
                'consensus_total',
                'heterozygosity',
                'nloci',
            ],
        )
        for sname in stats.index:
            sample = self.samples[sname]

            # ref does only step7
            if sname == "reference":
                if sample.stats_s7:
                    stats.loc[sname, 'nloci'] = sample.stats_s7.nloci
                continue

            # other samples do all steps.
            stats.loc[sname, 'state'] = sample.state
            stats.loc[sname, 'reads_raw'] = sample.stats_s1.reads_raw

            if sample.stats_s2:
                value = sample.stats_s2.reads_passed_filter
            else:
                value = pd.NA
            stats.loc[sname, 'reads_passed_filter'] = value

            if sample.stats_s3:
                value = sample.stats_s3.clusters_total
            else:
                value = pd.NA
            stats.loc[sname, 'clusters_total'] = value

            if sample.stats_s3:
                value = sample.stats_s3.clusters_hidepth
            else:
                value = pd.NA
            stats.loc[sname, 'clusters_hidepth'] = value

            if sample.stats_s3:
                value = sample.stats_s3.mean_depth_total
            else:
                value = pd.NA
            stats.loc[sname, 'mean_depth_total'] = value

            if sample.stats_s3:
                value = sample.stats_s3.reads_mapped_to_ref_prop
            else:
                value = pd.NA
            stats.loc[sname, 'reads_mapped_to_ref_prop'] = value

            if sample.stats_s5:
                value = sample.stats_s5.consensus_total
            else:
                value = pd.NA
            stats.loc[sname, 'consensus_total'] = value

            if sample.stats_s5:
                value = sample.stats_s5.heterozygosity
            else:
                value = pd.NA
            stats.loc[sname, 'heterozygosity'] = value

            if sample.stats_s7:
                value = sample.stats_s7.nloci
            else:
                value = pd.NA
            stats.loc[sname, 'nloci'] = value

        # drop columns that are all NAN
        stats = stats.dropna(axis=1, how="all")
        return stats

    def branch(self, name:str, subsample:List[str]=None) -> 'Assembly':
        """Return a new branched Assembly class instance.

        The new object will have the same parameter settings as the
        current object, and inherits the same sample histories, but
        will write to a different name prefix path going forward.

        Creating a new branch does not write a JSON until you either
        run an Assembly step by calling .run() or call .save_json()

        Examples
        --------
        >>> data1 = ip.Assembly("data1")
        >>> data1.params.sorted_fastq_path = "./data/*.gz"
        >>> data1.run('1')
        >>> data2 = data1.branch("data2", subsample=['1A_0', '1B_0'])
        >>> data2.run('2')
        """
        # create new names Assembly and copy over all params except name
        branch = Assembly(name)
        params = self.params.dict()
        params['assembly_name'] = name
        branch.params = ParamsSchema(**params)
        branch.hackers = HackersSchema(**self.hackers.dict())

        # copy over all or just a subsamples of the samples.
        if subsample is None:
            branch.samples = {
                i: SampleSchema(**self.samples[i].dict()) for i in self.samples
            }
        else:
            branch.samples = {
                i: SampleSchema(**self.samples[i].dict())
                for i in self.samples if i in subsample
            }
            for i in subsample:
                if i not in self.samples:
                    logger.warning(f"sample name {i} does not exist.")
        return branch

    def write_params(self, force:bool=False) -> None:
        """Write a CLI params file to <workdir>/params-<name>.txt.

        Writes the current Params for this Assembly. When this is 
        called from the CLI as `ipyrad -n name` it writes to the 
        current directory, since project_dir has not been created yet,
        which is fine, since user's should only need to call it once
        when using the CLI, probably.
        """
        outname = f"params-{self.name}.txt"
        outpath = self.params.project_dir / outname

        # Test if params file already exists?
        # If not forcing, test for file and bail out if it exists
        if not force:
            if outpath.exists():
                raise IPyradError(
                    f"file {outpath} exists, you must use force to overwrite")

        params = self.params.dict()
        with open(outpath, 'w', encoding="utf-8") as out:
            print("---------- ipyrad params file " + "-" * 80, file=out)
            for idx, param in enumerate(params):
                value = params.get(param)
                if isinstance(value, (tuple, list)):
                    value = ", ".join(map(str, value))
                else:
                    value = str(value) if value else ""
                print(
                    f"{value.ljust(40)} ## [{idx}] {param}: {PARAMSINFO[idx]}",
                    file=out,
                )
            logger.debug(f"params file written to {outpath}")

    def save_json(self) -> None:
        """Writes the current Assembly object to the project JSON file."""
        project = Project(
            params=ParamsSchema(**self.params.dict()),
            hackers=HackersSchema(**self.hackers.dict()),
            samples=self.samples,
            populations=self.populations,
        )
        with open(self.json_file, 'w', encoding="utf-8") as out:
            out.write(project.json(indent=2, exclude_none=True))
        logger.debug(f"wrote to {self.json_file}")

    def run(
        self,
        steps: str,
        cores: Optional[int]=None,
        force: bool=False,
        quiet: bool=False,
        ipyclient: Optional["ipyparallel.Client"]=None,
        **ipyclient_kwargs,
        ) -> None:
        """Run one or more assembly steps (1-7) of an ipyrad assembly.

        Parameters
        ----------
        steps: str
            A string of steps to run, e.g., "1", or "123".
        force: bool
            Force overwrite of existing results for this step.
        quiet: bool
            Suppress printed headers to stdout.
        ipyclient: None or ipyparallel.Client
            Optional ipyparallel client to connect to for distributing
            jobs in parallel. This option is generally only useful if
            you start a Client using MPI to connect to multiple nodes
            of an HPC cluster. See ipyrad HPC docs for details.

        Examples
        --------
        >>> data = ip.load_json("test.json")
        >>> data.run("123", cores=4)
        """
        # save the current JSON file (and a backup?)
        self.save_json()

        # init the ipyparallel cluster class wrapper
        if ipyclient is not None:
            raise NotImplementedError(
                "Usage of an external ipyclient is currently deprecated.")

        # init first step before starting cluster to check for
        # simple errors like missing file paths.
        # STEP_MAP[steps[0]](self, force=force, quiet=True, ipyclient=None)

        # start cluster asynchronously, run jobs, and shutdown.
        with Cluster(cores=cores, **ipyclient_kwargs) as client:
            # use client for any/all steps of assembly
            for step in steps:
                tool = STEP_MAP[step](self, force, quiet, client)
                tool.run()
                # shutil.rmtree(tool.tmpdir)  # uncomment when not testing.

    async def _run_async(
        self,
        steps: str,
        cores: Optional[int]=None,
        force: bool=False,
        quiet: bool=False,
        ipyclient: Optional["ipyparallel.Client"]=None,
        **ipyclient_kwargs,
        ) -> None:
        """Run one or more assembly steps (1-7) of an ipyrad assembly.

        This starts and shutsdown the ipyparallel cluster asynchronously
        (faster). It is currently only designed for use with in the CLI,
        not in the Python API, and will cause problems in jupyter, thus
        it is a private func.

        FIXME: interrupt not working yet for this func...?

        Parameters
        ----------
        steps: str
            A string of steps to run, e.g., "1", or "123".
        force: bool
            Force overwrite of existing results for this step.
        quiet: bool
            Suppress printed headers to stdout.
        ipyclient: None or ipyparallel.Client
            Optional ipyparallel client to connect to for distributing
            jobs in parallel. This option is generally only useful if
            you start a Client using MPI to connect to multiple nodes
            of an HPC cluster. See ipyrad HPC docs for details.

        Examples
        --------
        >>> data = ip.load_json("test.json")
        >>> data.run("123", cores=4)
        """
        # save the current JSON file (and a backup?)
        self.save_json()

        # init the ipyparallel cluster class wrapper
        if ipyclient is not None:
            raise NotImplementedError(
                "Usage of an external ipyclient is currently deprecated.")

        # init first step before starting cluster to check for
        # simple errors like missing file paths.
        STEP_MAP[steps[0]](self, force=force, quiet=True, ipyclient=None)

        # start cluster asynchronously, run jobs, and shutdown.
        async with Cluster(cores=cores, **ipyclient_kwargs) as client:
            # use client for any/all steps of assembly
            for step in steps:
                tool = STEP_MAP[step](self, force, quiet, client)
                tool.run()
                # shutil.rmtree(tool.tmpdir)  # uncomment when not testing.


# the Class functions to run for each entered step.
STEP_MAP = {
    "1": Step1,
    "2": Step2,
    "3": Step3,
    "4": Step4,
    "5": Step5,
    "6": Step6,
    "7": Step7,
}

# PARAMS FILE INFO WRITTEN TO CLI PARAMS FILE.
PARAMSINFO = {
    0: "Prefix name for output files",
    1: "Output file path (created if absent)",
    2: "Path to non-demultiplexed fastq data",
    3: "Path to barcodes file",
    4: "Path to a demultiplexed fastq data",
    5: "Assembly method ('denovo' or 'reference')",
    6: "Path to a reference genome fasta file",
    7: "Datatype (rad, gbs, pairddrad, etc)",
    8: "One or two sequences viewable in read data",
    9: "Bases with Q<20",
    10: "33 is default, much older data may be 64",
    11: "Cutoff for making statistical base calls",
    12: "Only used if < min_depth_statistical",
    13: "Clusters with > max_depth are excluded",
    14: "Sequence similarity cutoff for denovo clustering",
    15: "Matching cutoff for demultiplexing",
    16: "2=default, 1=only quality filtering, 0=no filtering",
    17: "Reads shorter after trimming are excluded",
    18: "Consensus quality filter (max integer)",
    19: "Consensus quality filter (max proportion)",
    20: "Consensus quality filter (max proportion)",
    21: "Locus quality filter (min integer)",
    22: "Locus quality filter (max proportion)",
    23: "Locus quality filter (max integer)",
    24: "Locus quality filter (max proportion)",
    25: "Pre-align trim edges (R1>, <R1, R2>, <R2)",
    26: "Post-align trim edges (R1>, <R1, R2>, <R2)",
    27: "See documentation",
    28: "Path to population assignment file",
    29: "Reads mapped to this reference fasta are removed",
}



if __name__ == "__main__":

    import ipyrad as ip
    ip.set_log_level("DEBUG")#, logfile="/tmp/test.log")

    TEST = ip.Assembly("PEDIC2")
    TEST.params.project_dir = "/tmp"
    TEST.params.sorted_fastq_path = "../../sra-fastqs/*.fastq"
    TEST.populations['all'] = (['a', 'b', 'c'], 3)
    TEST.write_params(True)
    print((TEST.params.project_dir))

    # TEST.run('1', force=True, quiet=True)
    # print(TEST.stats)
    # print(TEST.outfiles)
    # print(TEST.assembly_stats)


    # TEST = ip.Assembly("TEST1")
    # TEST.params.raw_fastq_path = "../../tests/ipsimdata/rad_example_R1*.gz"
    # TEST.params.barcodes_path = "../../tests/ipsimdata/rad_example_barcodes.txt"
    # TEST.params.project_dir = "/tmp"
    # TEST.params.max_barcode_mismatch = 1
    # TEST.run('1', force=True, quiet=True)

    # data = ip.Assembly('TEST')
    # data.params.project_dir = "/tmp"
    # data.params.raw_fastq_path = "../../tests/ipsimdata/rad_example_R1*.fastq.gz"
    # data.params.barcodes_path = "../../tests/ipsimdata/rad_example_barcodes.txt"
    # data.run("1", force=True, quiet=True)
    # print(data.stats)

