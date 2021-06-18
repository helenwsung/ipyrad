#!/usr/bin/env python

"""
Assembly class object as the main API for calling assembly steps.
"""

import os
import shutil
import traceback
from typing import List, Optional
from loguru import logger
import pandas as pd
from ipyparallel import Client
from ipyrad.core.params_schema import ParamsSchema, HackersSchema
from ipyrad.core.schema import Project, SampleSchema
from ipyrad.core.parallel import Cluster
from ipyrad.assemble.utils import IPyradError
from ipyrad.assemble.s1_demux import Step1
from ipyrad.assemble.s2_trim_reads import Step2
from ipyrad.assemble.s3_clustmap_within import Step3
from ipyrad.assemble.s4_joint_estimate import Step4
from ipyrad.assemble.s5_consensus import Step5
from ipyrad.assemble.s6_clustmap_across import Step6
from ipyrad.assemble.s7_assemble import Step7


class Assembly:
    """
    Returns a new Assembly class instance with default parameter
    settings. The first steps of an API analysis typically involves
    initializing a new Assembly, setting params, and then calling
    self.run() to start running assembly steps.
    """
    def __init__(self, name:str):

        # core JSON file components
        self.params = ParamsSchema(assembly_name=name)
        self.hackers = HackersSchema()
        self.samples = {}

        # optional dict for setting cluster config.
        self.ipcluster = {
            "cores": 0,
            "threads": 2,
        }

    def __repr__(self):
        return "<ipyrad.Assembly object {}>".format(self.name)

    @property
    def name(self):
        """shortcut to return the assembly_name from params"""
        return self.params.assembly_name
    
    @property
    def json_file(self):
        """JSON file is project_dir/assembly_name.json"""
        return os.path.join(
            self.params.project_dir, 
            self.params.assembly_name + ".json"
        )

    @property
    def stats(self):
        """
        Returns a dataframe with *summarized stats* extracted from the 
        project JSON file given the current completed assembly steps.
        To see more detailed stats from specific steps see instead 
        the self.stats_dfs.
        """
        # TODO: add step7 stats like nloci?

        # using self object (any cases where we need to load from json?)
        # self.save_json
        # proj = Project.parse_file(self.json_file)
        proj = self

        # dataframe to fill
        stats = pd.DataFrame(
            index=sorted(proj.samples),
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
            ],
        )
        for sname in stats.index:
            sample = proj.samples[sname]
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

        # drop columns that are all NAN
        stats = stats.dropna(axis=1, how="all")

        # FIXME: keep adding summary stats
        return stats
    

    def branch(self, name:str, subsample:List[str]=None) -> 'Assembly':
        """
        Returns a new branched Assembly class instance.

        The new object will have the same parameter settings as the 
        current object, and inherits the same sample histories, but 
        will write to a different name prefix path
        going forward. 

        Creating a new branch does not write a JSON until you either 
        run an Assembly step by calling .run() or call .save_json()

        Examples:
        ---------
        data1 = ip.Assembly("data1")
        data1.params.sorted_fastq_path = "./data/*.gz"
        data1.run('1')
        data2 = data1.branch("data2", subsample=['1A_0', '1B_0'])
        data2.run('2')
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
        """
        Write a CLI params file to <workdir>/params-<name>.txt with
        the current params in this Assembly.
        """
        outfile = f"params-{self.name}.txt"

        # Test if params file already exists?
        # If not forcing, test for file and bail out if it exists
        if not force:
            if os.path.exists(outfile):
                raise IPyradError(
                    f"file {outfile} exists, you must use force to overwrite")

        params = self.params.dict()
        with open(outfile, 'w') as out:
            print("---------- ipyrad params file " + "-" * 80, file=out)
            for idx, param in enumerate(params):
                value = params.get(param)
                if isinstance(value, (tuple, list)):
                    value = ", ".join(map(str, value))
                else:
                    value = str(value) if value else ""
                print(
                    f"{value.ljust(40)}## [{idx}] {param}: {PARAMSINFO[idx]}",
                    file=out,
                )


        # with open(outfile, 'w') as paramsfile:
        #     # Write the header. Format to 80 columns
        #     header = "------- ipyrad params file (v.{})".format(ip.__version__)
        #     header += ("-" * (80 - len(header)))
        #     paramsfile.write(header + "\n")

        #     # Whip through the current params and write out the current
        #     # param value, the ordered dict index number. Also,
        #     # get the short description from paramsinfo. 
        #     # Make it look pretty, with padding.
        #     params_string = []
        #     for key in self.params._keys:
        #         val = getattr(self.params, key)

        #         # If multiple elements, write them out comma separated
        #         if isinstance(val, list) or isinstance(val, tuple):
        #             paramvalue = ", ".join([str(i) for i in val])
        #         else:
        #             paramvalue = str(val)

        #         padding = (" " * (30 - len(paramvalue)))
        #         paramkey = self.params._keys.index(key)
        #         paramindex = " ## [{}] ".format(paramkey)
        #         name = "[{}]: ".format(paramname(paramkey))
        #         description = paraminfo(paramkey, short=True)
        #         params_string.append(
        #             "".join([
        #                 paramvalue,
        #                 padding,
        #                 paramindex,
        #                 name,
        #                 description])
        #             )
        #     # write the params string
        #     paramsfile.write("\n".join(params_string) + "\n")        # TODO


    def save_json(self) -> None:
        """
        Save the current Assembly object to the project JSON file  
        (<project_dir>/<name>.json)
        """
        project = Project(
            params=ParamsSchema(**self.params.dict()),
            hackers=HackersSchema(**self.hackers.dict()),
            samples={sname: self.samples[sname] for sname in self.samples},
        )
        with open(self.json_file, 'w') as out:
            out.write(project.json(indent=4, exclude_none=True))
        logger.debug(f"wrote to {self.json_file}")


    def run(
        self, 
        steps:str, 
        force:bool=False,
        quiet:bool=False,
        ipyclient:Optional[Client]=None,
        **kwargs,
        ) -> None:
        """
        Run one or more assembly steps (1-7) of an ipyrad assembly.

        Parameters
        ----------
        steps: str
            A string of steps to run, e.g., "1", or "123".
        force: bool
            Force overwrite of existing results for this step.
        quiet: bool
            Suppress printed outputs to stdout.
        ipyclient: Optional[ipyparallel.Client]
            Optional ipyparallel client to connect to for distributing
            jobs in parallel. This option is generally only useful if
            you start a Client using MPI to connect to multiple nodes
            of an HPC cluster. Otherwise, just configure the local 
            cluster parallelization using the .ipcluster attribute.
        """
        # save the current JSON file (and a backup?)
        self.save_json()

        # the Class functions to run for each entered step.
        step_map = {
            "1": Step1,
            "2": Step2, 
            "3": Step3,
            "4": Step4,
            "5": Step5, 
            "6": Step6,
            "7": Step7,
        }

        # could load the tool to check whether this job can be run 
        # before starting the ipcluster?...


        # init the ipyparallel cluster class wrapper
        cluster = Cluster(quiet=quiet)
        try:
            # establish connection to a new or running ipyclient
            cores = kwargs.get('cores', self.ipcluster['cores'])
            cluster.start(cores=cores, ipyclient=ipyclient)

            # use client for any/all steps of assembly 
            for step in steps:
                tool = step_map[step](self, force, quiet, cluster.ipyclient)
                tool.run()
                # shutil.rmtree(tool.tmpdir)   # uncomment when not testing.

        except KeyboardInterrupt:
            logger.warning("keyboard interrupt by user, cleaning up.")

        # AssemblyProgressBar logs the traceback
        except IPyradError as inst:
            logger.error(f"An error occurred:\n{inst}")
            print("An error occurred, see logfile and below.")
            raise

        # logger.error logs the traceback
        except Exception as inst:
            logger.error(
                "An unexpected error occurred, see logfile "
                f"and trace:\n{traceback.format_exc()}")
            raise

        finally:
            cluster.cleanup_safely(None)


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
    ip.set_loglevel("DEBUG", logfile="/tmp/test.log")

    TEST = ip.Assembly("PEDIC2")
    TEST.params.sorted_fastq_path = "../../sra-fastqs/*.fastq"
    TEST.write_params(True)
    # TEST.params.project_dir = "/tmp"
    # TEST.run('12', force=True, quiet=True)
    # print(TEST.stats)

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

