#!/usr/bin/env python

"""
In silico digest of a fasta genome file to fastq format data files.
"""

import os
import gzip
from typing import Optional
from ipyrad.assemble.utils import comp


class DigestGenome:
    """
    Digest a fasta genome file with one or two restriction enzymes 
    to create pseudo-fastq files to treat as samples in a RAD assembly. 

    Parameters
    ----------
    fasta (str):
        Path to a fasta genome file (optionally gzipped).

    workdir (str):
        Directory in which to write output fastq files. Will be 
        created if it does not yet exist.

    name (str):
        Name prefix for output files.

    readlen (int):
        The length of the sequenced read extending from the cut 
        site when creating fastq reads from the digested fragments.

    re1 (str):
        First restriction enzyme recognition site. Note, this can be
        different from the sequence you may provide in ipyrad assembly
        which is the sequence after ligation to a compatible end.

    re2 (str):
        Second restriction enzyme recognition site. See re1 note.

    ncopies (int):
        The number of copies to make for every digested copy to write
        to as fastq reads in the output files.

    nscaffolds (int, None):
        Only the first N scaffolds (sorted in order from longest to 
        shortest) will be digested. If None then all scaffolds are 
        digested.

    Example:
    --------
    tool = ipa.digest_genome(
        fasta="genome.fa", 
        workdir="digested_genomes",
        name="quinoa",
        re1="CCGG",      # mspI
        re2="AAGCTT",    # hindIII
        ncopies=5,
        readlen=150,
        paired=True,        
    )
    tool.run()

    """
    def __init__(
        self, 
        fasta: str, 
        name: str="digested", 
        workdir: str="digested_genomes",
        re1: str="CTGCAG", 
        re2: Optional[str]=None, 
        ncopies: int=1,
        readlen: int=150, 
        paired: bool=True, 
        min_size: Optional[int]=None, 
        max_size: Optional[int]=None,
        nscaffolds: Optional[int]=None,
        ):

        self.fasta = fasta
        self.name = name
        self.workdir = workdir
        self.re1 = re1
        self.re2 = re2
        self.ncopies = ncopies
        self.readlen = readlen
        self.paired = paired
        self.min_size = min_size
        self.max_size = max_size
        self.nscaffolds = nscaffolds

        # use readlen as min_size if not entered
        if not self.min_size:
            self.min_size = self.readlen
        if not self.max_size:
            self.max_size = 9999999


    def run(self):
        """
        Parses the genome into scaffolds list and then cuts each into digested
        chunks and saves as fastq.
        """
        # counter
        iloc = 0

        # open output file for writing
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
        handle1 = os.path.join(self.workdir, self.name + "_R1.fastq.gz")
        handle2 = os.path.join(self.workdir, self.name + "_R2.fastq.gz")

        out1 = gzip.open(handle1, 'w')
        if self.paired:
            out2 = gzip.open(handle2, 'w')

        # load genome file
        if self.fasta.endswith(".gz"):
            fio = gzip.open(self.fasta)
            scaffolds = fio.read().decode().split(">")[1:]
        else:
            fio = open(self.fasta)
            scaffolds = fio.read().split(">")[1:]

        # add revcomp of every scaffold
        # rscaffs = []
        # for scaff in scaffolds:
            # name, seq = scaff.split("\n", 1)            
            # rscaffs.append(f"{name}\n{comp(seq)[::-1]}")
        # scaffolds = scaffolds + rscaffs

        # sort scaffolds by length
        scaffolds = sorted(scaffolds, key=len, reverse=True)

        # iterate over scaffolds
        for scaff in scaffolds[:self.nscaffolds]:

            # get name 
            name, seq = scaff.split("\n", 1)

            # no funny characters in names plz
            name = name.replace(" ", "_").strip()

            # makes seqs nice plz
            seq = seq.replace("\n", "").upper()

            # digest scaffold into fragments and discard scaff ends
            bits = ["1{}1".format(i) for i in seq.split(self.re1)][1:-1]

            # digest each fragment into second cut fragment
            if not self.re2:
                bits1 = []
                for fragment in bits:
                    if len(fragment) > self.min_size:
                        # forward read on fragment
                        bits1.append((fragment[1:-1], 0, self.max_size))
                bits = bits1

            else:
                bits1 = bits
                bits = []
                pos = 0
                for fragment in bits1:
                    fbits = fragment.split(self.re2)

                    if len(fbits) > 1:
                        # remove the 1
                        fbit = fbits[0][1:]        # 1----2
                        rbit = fbits[-1][:-1]      # 2----1

                        lef = len(fbit)
                        if self.max_size >= lef >= self.min_size:
                            bits.append((fbit, pos, lef))
                        # if (lef > self.min_size) and (lef <= self.max_size):
                            #pos = seq.index(fbit)

                        lef = len(rbit)
                        if self.max_size >= lef >= self.min_size:                        
                            #res = comp(rbit)[::-1]
                            res = rbit
                            bits.append((res, pos, lef))
                            # bits.append((rbit, pos, lef))

            # turn fragments into (paired) reads
            fastq_r1s = []
            fastq_r2s = []            

            for fragment in bits:
                fragment, pos, _ = fragment
                read1 = fragment[:self.readlen]
                read2 = comp(fragment[-self.readlen:])[::-1]

                # write reads to a file
                for copy in range(self.ncopies):
                    fastq = "@{name}_loc{loc}_rep{copy} 1:N:0:\n{read}\n+\n{qual}"
                    fastq = fastq.format(**{
                        'name': name,
                        'loc': iloc,
                        'copy': copy,
                        'read': read1, 
                        'qual': "B" * len(read1),
                    })
                    fastq_r1s.append(fastq)

                    if self.paired:                   
                        fastq = "@{name}_loc{loc}_rep{copy} 2:N:0:\n{read}\n+\n{qual}"
                        fastq = fastq.format(**{
                            'name': name,
                            'loc': iloc, 
                            'copy': copy,
                            'read': read2,
                            'qual': "B" * len(read2),
                        })
                        fastq_r2s.append(fastq)
                iloc += 1

            # write all bits of scaffold to disk
            if fastq_r1s:
                out1.write(("\n".join(fastq_r1s)).encode() + b"\n")
                if self.paired:
                    out2.write("\n".join(fastq_r2s).encode() + b"\n")

        # close handles
        fio.close()
        out1.close()
        if self.paired:
            out2.close()

        # report stats
        print("extracted reads from {} positions".format(iloc))
