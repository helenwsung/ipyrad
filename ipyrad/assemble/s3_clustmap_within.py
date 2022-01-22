#!/usr/bin/env python

"""Starts either a denovo or reference based job.

Build stacks of reads from the same loci wtihin samples by clustering
or mapping to a reference.
"""

from ipyrad.assemble.base_step import BaseStep
from ipyrad.assemble.clustmap_within_denovo import ClustMapDenovo
from ipyrad.assemble.clustmap_within_reference import ClustMapReference


class Step3(BaseStep):
    """Run Step3 clustering/mapping using vsearch or bwa
    """
    def __init__(self, data, force, quiet, ipyclient):
        super().__init__(data, 3, quiet, force)
        self.ipyclient = ipyclient
        self.lbview = self.ipyclient.load_balanced_view()
        self.thview = self.ipyclient.load_balanced_view(
            ipyclient.ids[:self.data.ipcluster['threads']]
        )

    def run(self):
        """Submit jobs to run either denovo, reference, or complex."""
        if self.data.params.assembly_method == "denovo":
            ClustMapDenovo(self).run()
        else:
            ClustMapReference(self).run()


if __name__ == "__main__":

    import ipyrad as ip
    ip.set_log_level("INFO")
   
    # TEST = ip.Assembly("TEST1")
    # TEST.params.raw_fastq_path = "../../tests/ipsimdata/rad_example_R1*.gz"    
    # TEST.params.barcodes_path = "../../tests/ipsimdata/rad_example_barcodes.txt"
    # TEST.params.project_dir = "/tmp"
    # TEST.params.max_barcode_mismatch = 1
    # TEST.params.assembly_method = "reference"   
    # TEST.params.reference_sequence = "../../tests/ipsimdata/rad_example_genome.fa"
    # TEST.run('3', force=True, quiet=True)

    # SE DATA
    # TEST = ip.load_json("/tmp/TEST1.json")
    # TEST.run("123", force=True, quiet=True)

    # simulated PE DATA DENOVO
    # TEST = ip.load_json("/tmp/TEST5.json")
    # TEST.run("3", force=True, quiet=True)

    # simulated PE DATA REFERENCE
    TEST = ip.load_json("/tmp/TEST5.json")
    TEST.params.assembly_method = "reference"
    TEST.params.reference_sequence = "../../tests/ipsimdata/pairddrad_example_genome.fa"
    TEST.run("3", force=True, quiet=True)

    # Empirical SE
    # TEST = ip.load_json("/tmp/PEDIC.json")
    # TEST.run("123", force=True)



    # DATA = DATA.branch("TEST6")
    # DATA.params.datatype = "pairddrad"
    # DATA.params.assembly_method = "denovo"
    # DATA.params.reference_sequence = "../../tests/ipsimdata/rad_example_genome.fa"
    # DATA.run("123", force=True, quiet=True)
    # step = Step3(DATA, 1, 0, CLIENT)
    # print(len(step.samples))
