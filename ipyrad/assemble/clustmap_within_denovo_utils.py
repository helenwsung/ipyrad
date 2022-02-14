#!/usr/bin/env python

"""Functions run on remote parallel cluster in clustmap.

"""

import os
import sys
import gzip
import glob
import subprocess
from itertools import islice, chain
import numpy as np
from loguru import logger
from ipyrad.assemble.utils import IPyradError, comp
from ipyrad.core.schema import Stats3

logger = logger.bind(name="ipyrad")

BIN_MUSCLE = os.path.join(sys.prefix, "bin", "muscle")
BIN_VSEARCH = os.path.join(sys.prefix, "bin", "vsearch")

def concat_multiple_edits(data, sample):
    """Create a temporary concatenated file for multiple edits input
    files, which arises when Assemblies were merged between steps
    2 and 3.
    """
    # define output files
    concat1 = os.path.join(data.tmpdir, f"{sample.name}_concat_R1.fastq.gz")
    concat2 = os.path.join(data.tmpdir, f"{sample.name}_concat_R2.fastq.gz")

    read1s = [i[0] for i in sample.files.edits]
    if len(read1s) > 1:
        cmd = ['cat'] + read1s
        with open(concat1, 'w', encoding="utf-8") as cout:
            with subprocess.Popen(
                cmd, stderr=subprocess.PIPE, stdout=cout, close_fds=True,
                ) as proc:
                res = proc.communicate()[1]
                if proc.returncode:
                    raise IPyradError(f"cmd: {' '.join(cmd)}\nerror: {res}")                

    read2s = [i[1] for i in sample.files.edits if i[1]]
    if len(read2s) > 1:
        cmd = ['cat'] + read2s
        with open(concat2, 'w', encoding="utf-8") as cout:
            with subprocess.Popen(
                cmd, stderr=subprocess.PIPE, stdout=cout, close_fds=True,
                ) as proc:
                res = proc.communicate()[1]
                if proc.returncode:
                    raise IPyradError(f"cmd: {' '.join(cmd)}\nerror: {res}")

def merge_pairs_with_vsearch(data, sample):
    """Merge PE reads using vsearch to find overlap."""
    # input files (select only the top one)
    in1 = [
        os.path.join(data.tmpdir, f"{sample.name}_unmapped_R1.fastq"),
        os.path.join(data.tmpdir, f"{sample.name}_concat_R1.fq.gz"),
        sample.files.edits[0][0],        
    ]
    in2 = [
        os.path.join(data.tmpdir, f"{sample.name}_unmapped_R2.fastq"),
        os.path.join(data.tmpdir, f"{sample.name}_concat_R2.fq.gz"),
        sample.files.edits[0][1],
    ]
    index = min([i for i, j in enumerate(in1) if os.path.exists(j)])
    infile1 = in1[index]
    infile2 = in2[index]

    # define output files
    mergedfile = os.path.join(data.tmpdir, f"{sample.name}_merged.fastq")
    nonmerged1 = os.path.join(data.tmpdir, f"{sample.name}_nonmerged_R1_.fastq")
    nonmerged2 = os.path.join(data.tmpdir, f"{sample.name}_nonmerged_R2_.fastq")

    # get the maxn and minlen values
    try:
        maxn = sum(data.params.max_low_qual_bases)
    except TypeError:
        maxn = data.params.max_low_qual_bases
    minlen = str(max(32, data.params.filter_min_trim_len))

    # vsearch merge can now take gzipped files (v.2.8)
    cmd = [
        BIN_VSEARCH,
        "--fastq_mergepairs", infile1,
        "--reverse", infile2,
        "--fastqout", mergedfile,
        "--fastqout_notmerged_fwd", nonmerged1,
        "--fastqout_notmerged_rev", nonmerged2,
        "--fasta_width", "0",
        "--fastq_minmergelen", minlen,
        "--fastq_maxns", str(maxn),
        "--fastq_minovlen", "20",
        "--fastq_maxdiffs", "4",
        "--label_suffix", "_m1",
        "--fastq_qmax", "93",     # <- Set high to allow FASTQ+64
        "--threads", "2",
        "--fastq_allowmergestagger",
    ]
    # send to logger.debug
    # logger.debug(" ".join(cmd))
    with subprocess.Popen(cmd, 
        stderr=subprocess.STDOUT, stdout=subprocess.PIPE) as proc:
        res = proc.communicate()[0].decode()
        if proc.returncode:
            logger.exception(res)
            raise IPyradError("Error merge pairs:\n {}\n{}".format(cmd, res))
    return cmd

def merge_end_to_end(data, sample, revcomp, append, identical=False):
    """Combines read1 and read2 with a 'nnnn' separator. If the data are 
    going to be refmapped then do not revcomp the read2. 

    Parameters
    ----------
    identical: bool
        *only for paired denovo refminus*
        It will split paired reads that have already been
        merged by vsearch. In this case it does not split them, but just uses
        the full seq as both R1 and R2. When joining them back we will join 
        other reads with nnnn, but if R1 and R2 are identical then we keep 
        just the R1 as the merged readpair. 
    """
    if identical:
        mergedfile = os.path.join(data.tmpdir, f"{sample.name}_remerged.fa")
    else:
        mergedfile = os.path.join(data.tmpdir, f"{sample.name}_merged.fastq")

    # input file options
    altmapped1 = os.path.join(data.tmpdir, f"{sample.name}_unmapped_R1.fastq")
    altmapped2 = os.path.join(data.tmpdir, f"{sample.name}_unmapped_R2.fastq")
    nonmerged1 = os.path.join(data.tmpdir, f"{sample.name}_nonmerged_R1.fastq")
    nonmerged2 = os.path.join(data.tmpdir, f"{sample.name}_nonmerged_R2.fastq")
    concat1 = os.path.join(data.tmpdir, f"{sample.name}_concat_edits.fastq.gz")
    concat2 = os.path.join(data.tmpdir, f"{sample.name}_concat_edits.fastq.gz")

    # data.dirs.edits doesn't exist if you merge after step 2, so
    # here we access the edits files through the sample object.
    # Sorry it makes the code less harmonious. iao 12/31/19.
    edits1 = sample.files.edits[0][0]
    edits2 = sample.files.edits[0][1]

    # file precedence
    order1 = (edits1, concat1, nonmerged1, altmapped1)
    order2 = (edits2, concat2, nonmerged2, altmapped2)
    nonm1 = [i for i in order1 if os.path.exists(i)][-1]
    nonm2 = [i for i in order2 if os.path.exists(i)][-1]

    # Combine the unmerged pairs and append to the merge file
    if append:
        combout = open(mergedfile, 'at')
    else:
        combout = open(mergedfile, 'wt')

    # read in paired end read files 4 lines at a time
    if nonm1.endswith(".gz"):
        fr1 = gzip.open(nonm1, 'rt')
    else:
        fr1 = open(nonm1, 'rt')
    quart1 = zip(*[fr1] * 4)

    if nonm2.endswith(".gz"):
        fr2 = gzip.open(nonm2, 'rt')
    else:
        fr2 = open(nonm2, 'rt')
    quart2 = zip(*[fr2] * 4)
    quarts = zip(quart1, quart2)

    # a list to store until writing
    writing = []
    counts = 0

    # iterate until done
    while 1:
        try:
            read1s, read2s = next(quarts)
        except StopIteration:
            break

        # [paired-denovo-refminus option] do not join truly merged reads
        if identical:
            # keep already merged r1 and the read, or combine with nnnn
            if read1s[1] == read2s[1]:
                newread = [">" + read1s[0][1:], read1s[1]]
            else:
                newread = [
                    ">" + read1s[0][1:],
                    read1s[1].strip() + "nnnn" + read2s[1].strip() + "\n",
                ]
            writing.append(b"".join(newread))

        # the standard pipeline
        else:
            # revcomp for denovo data
            if revcomp:
                writing.append("".join([
                    read1s[0],
                    read1s[1].strip() + "nnnn" + (
                        comp(read2s[1].strip()[::-1]) + "\n"),
                    read1s[2],
                    read1s[3].strip() + "nnnn" + (
                        read2s[3].strip()[::-1] + "\n"),
                    ]))

            # no revcomp for reference mapped data
            else:
                writing.append("".join([
                    read1s[0],
                    read1s[1].strip() + "nnnn" + (
                        read2s[1]),
                    read1s[2],
                    read1s[3].strip() + "nnnn" + (
                        read2s[3]),
                    ]))

        # keep count
        counts += 1
        if not counts % 5000:
            combout.write("".join(writing))
            writing = []

    if writing:
        combout.write("".join(writing))

    # close handles
    fr1.close()
    fr2.close()
    combout.close()

def dereplicate(data, sample):
    """Dereplicate reads and sort so reads that were highly replicated are at
    the top, and singletons at bottom, writes output to derep file. Paired
    reads are dereplicated as one concatenated read and later split again.

    Updated this function to take infile and outfile to support the double
    dereplication that we need for 3rad (5/29/15 iao).
    """
    # select input file with following precedence:
    # .trimmed_R1.fastq.gz, .concat_edit.fq.gz, ._merged.fastq, ._declone.fastq
    infiles = [
        sample.files.edits[0][0],
        os.path.join(data.tmpdir, f"{sample.name}_concat_edits.fastq"),
        os.path.join(data.tmpdir, f"{sample.name}_merged.fastq"),
        os.path.join(data.tmpdir, f"{sample.name}_declone.fastq"),
    ]
    infiles = [i for i in infiles if os.path.exists(i)]
    infile = infiles[-1]

    # datatypes options
    strand = "plus"
    if data.params.datatype in ['gbs', 'pairgbs', '2brad']:
        strand = "both"

    # do dereplication with vsearch
    cmd = [
        BIN_VSEARCH,
        "--derep_fulllength", infile,
        "--strand", strand,
        "--output", os.path.join(data.tmpdir, sample.name + "_derep.fa"),
        "--fasta_width", str(0),
        "--minseqlength", str(data.params.filter_min_trim_len),
        "--sizeout", 
        "--relabel_md5",
        "--quiet",
        "--threads", str(4),
        # "--fastq_qmax", "1000",        
    ]

    # decompress argument (IF ZLIB is missing this will not work!!) 
    # zlib is part of the conda installation.
    if infile.endswith(".gz"):
        cmd.append("--gzip_decompress")

    # build PIPEd job
    with subprocess.Popen(cmd, 
        stderr=subprocess.STDOUT, stdout=subprocess.PIPE, close_fds=True
        ) as proc:
        errmsg = proc.communicate()[0]
        if proc.returncode:
            raise IPyradError(errmsg.decode())

def cluster(data, sample):
    """Calls vsearch for clustering. cov varies by data type, values were chosen
    based on experience, but could be edited by users."""
    # get dereplicated reads for denovo+reference or denovo-reference
    handles = [
        os.path.join(data.tmpdir, f"{sample.name}_derep.fa"),
        os.path.join(data.tmpdir, f"{sample.name}_remerged.fa"),
    ]
    derephandle = [i for i in handles if os.path.exists(i)][-1]
    assert os.path.exists(derephandle), "bad derep handle"

    # create handles for the outfiles
    uhandle = os.path.join(data.tmpdir, sample.name + ".utemp")
    temphandle = os.path.join(data.tmpdir, sample.name + ".htemp")

    # datatype specific optimization
    # minsl: the percentage of the seed that must be matched
    #    smaller values for RAD/ddRAD where we might want to combine, say 50bp
    #    reads and 100bp reads in the same analysis.
    # query_cov: the percentage of the query sequence that must match seed
    #    smaller values are needed for gbs where only the tips might overlap
    #    larger values for pairgbs where they should overlap near completely
    #    small minsl and high query cov allows trimmed reads to match to untrim
    #    seed for rad/ddrad/pairddrad.
    strand = "plus"
    cov = 0.5
    minsl = 0.5
    if data.params.datatype in ["gbs", "2brad"]:
        strand = "both"
        cov = 0.5
        minsl = 0.5
    elif data.params.datatype == 'pairgbs':
        strand = "both"
        cov = 0.75
        minsl = 0.75

    # If this value is not null (which is the default) then override query cov
    if data.hackers.query_cov:
        cov = str(data.hackers.query_cov)
        assert float(cov) <= 1, "query_cov must be <= 1.0"

    # get call string
    cmd = [
        BIN_VSEARCH,
        "-cluster_smallmem", derephandle,
        "-strand", strand,
        "-query_cov", str(cov),
        "-id", str(data.params.clust_threshold),
        "-minsl", str(minsl),
        "-userout", uhandle,
        "-userfields", "query+target+id+gaps+qstrand+qcov",
        "-maxaccepts", "1",
        "-maxrejects", "0",
        "-threads", str(max(1, data.ipcluster['threads'])),
        "-notmatched", temphandle,
        "-fasta_width", "0",
        "--minseqlength", str(data.params.filter_min_trim_len),
        # "-fastq_qmax", "100",
        "-fulldp",
        "-usersort",
    ]

    # run vsearch
    logger.debug(" ".join(cmd))
    with subprocess.Popen(cmd, 
        stderr=subprocess.STDOUT, stdout=subprocess.PIPE, close_fds=True,
        ) as proc:
        res = proc.communicate()[0]

    # check for errors
    if proc.returncode:
        raise IPyradError(f"cmd {cmd}: {res}")

def build_clusters(data, sample):
    """Combines information from .utemp and .htemp files to create .clust files,
    which contain un-aligned clusters. Hits to seeds are only kept in the
    cluster if the number of internal indels is less than 'maxindels'.
    By default, we set maxindels=6 for this step (within-sample clustering).
    """

    # If reference assembly then here we're clustering the unmapped reads
    # if "reference" in data.params.assembly_method:
    # derepfile = os.path.join(
    # data.tmpdir, sample.name + "-refmap_derep.fa")
    infiles = [
        os.path.join(data.tmpdir, f"{sample.name}_derep.fa"),
        os.path.join(data.tmpdir, f"{sample.name}_remerged.fa"),
    ]
    derepfile = [i for i in infiles if os.path.exists(i)][-1]

    # i/o vsearch files
    uhandle = os.path.join(data.tmpdir, f"{sample.name}.utemp")
    usort = os.path.join(data.tmpdir, f"{sample.name}.utemp.sort")
    hhandle = os.path.join(data.tmpdir, f"{sample.name}.htemp")
    clustsout = open(os.path.join(data.tmpdir, f"{sample.name}.clust.txt"), 'w')

    # Sort the uhandle file so we can read through matches efficiently
    cmd = ["sort", "-k", "2", uhandle, "-o", usort]
    proc = subprocess.Popen(cmd, close_fds=True)
    _ = proc.communicate()[0]

    # load ALL derep reads into a dictionary (this can be a few GB of RAM)
    # and is larger if names are larger. We are grabbing two lines at a time.
    alldereps = {}
    with open(derepfile, 'rt') as ioderep:
        dereps = zip(*[ioderep] * 2)
        for namestr, seq in dereps:
            nnn, sss = [i.strip() for i in (namestr, seq)]  
            alldereps[nnn[1:]] = sss

    # store observed seeds (this could count up to >million in bad data sets)
    seedsseen = set()

    # Iterate through the usort file grabbing matches to build clusters
    with open(usort, 'rt') as insort:
        # iterator, seed null, seqlist null
        isort = iter(insort)
        lastseed = 0
        fseqs = []
        seqlist = []
        seqsize = 0
        while 1:

            # grab the next line and parse the informative columns
            try:
                hit, seed, _, ind, ori, _ = next(isort).strip().split()
            except StopIteration:
                break

            # same seed, append match
            if seed != lastseed:
                seedsseen.add(seed)

                # store the last cluster (fseq), count it, and clear fseq
                if fseqs:
                    # sort hits by size, which is at the end of the header
                    fsort = sorted(
                        fseqs[1:], 
                        key=(lambda x: int(x.split(";size=")[-1].split("\n")[0][:-2])),
                        reverse=True,
                    )
                    # seed first then sorted hits
                    fseqs = [fseqs[0]] + fsort
                    seqlist.append("\n".join(fseqs))
                    seqsize += 1
                    fseqs = []

                # occasionally write/dump stored clusters to file and clear mem
                if not seqsize % 10000:
                    if seqlist:
                        clustsout.write(
                            "\n//\n//\n".join(seqlist) + "\n//\n//\n")
                        # reset list and counter
                        seqlist = []

                # store the new seed on top of fseq list
                fseqs.append(">{};*\n{}".format(seed, alldereps[seed]))
                lastseed = seed

            # add match to the seed
            # revcomp if orientation is reversed (comp preserves nnnn)
            if ori == "-":
                seq = comp(alldereps[hit])[::-1]
            else:
                seq = alldereps[hit]
            # only save if not too many indels
            if int(ind) <= data.max_indels:
                fseqs.append(">{};{}\n{}".format(hit, ori, seq))

    # write whatever is left over to the clusts file
    if fseqs:
        seqlist.append("\n".join(fseqs))
    if seqlist:
        clustsout.write("\n//\n//\n".join(seqlist) + "\n//\n//\n")

    # now write the seeds that had no hits. Make dict from htemp
    with open(hhandle, 'rt') as iotemp:
        nohits = zip(*[iter(iotemp)] * 2)
        seqlist = []
        seqsize = 0
        while 1:
            try:
                nnn, _ = [i.strip() for i in next(nohits)]
            except StopIteration:
                break

            # occasionally write to file
            if not seqsize % 10000:
                if seqlist:
                    clustsout.write("\n//\n//\n".join(seqlist) + "\n//\n//\n")
                    # reset list and counter
                    seqlist = []

            # append to list if new seed
            if nnn[1:] not in seedsseen:
                seqlist.append("{};*\n{}".format(nnn, alldereps[nnn[1:]]))
                seqsize += 1

    # write whatever is left over to the clusts file
    if seqlist:
        clustsout.write("\n//\n//\n".join(seqlist))

    # close the file handle
    clustsout.close()
    del alldereps


def muscle_chunker(data, sample):
    """
    Splits the muscle alignment into chunks. Each chunk is run on a separate
    computing core. Because the largest clusters are at the beginning of the 
    clusters file, assigning equal clusters to each file would put all of the 
    large cluster, that take longer to align, near the top. So instead we 
    randomly distribute the clusters among the files. If assembly method is
    reference then this step is just a placeholder and nothing happens. 
    """
    # only chunk up denovo data, refdata has its own chunking method which 
    # makes equal size chunks, instead of uneven chunks like in denovo
    clustfile = os.path.join(data.tmpdir, sample.name + ".clust.txt")

    # get the number of clusters and chunk into 20 pieces
    with iter(open(clustfile, 'rt')) as clustio:
        nloci = sum(1 for i in clustio if "//" in i) // 2
        optim = (nloci // 20) + (nloci % 20)

    # write optim clusters to each tmp file
    clustio = open(clustfile, 'rt')
    inclusts = iter(clustio.read().strip().split("//\n//\n"))

    # splitting loci so first file is smaller and last file is bigger
    inc = optim // 10
    for idx in range(10):
        # how big is this chunk?
        this = optim + (idx * inc)
        left = nloci - this
        if idx == 9:
            # grab everything left
            grabchunk = list(islice(inclusts, int(1e9)))
        else:
            # grab next chunks-worth of data
            grabchunk = list(islice(inclusts, this))
            nloci = left

        # write the chunk to file
        tmpfile = os.path.join(data.tmpdir, f"{sample.name}_chunk_{idx}.ali")
        with open(tmpfile, 'w') as out:
            out.write("//\n//\n".join(grabchunk))
            # out.write(str.encode("//\n//\n".join(grabchunk)))
    clustio.close()


def align_and_parse(handle, max_internal_indels=5, is_gbs=False, declone=False):
    """ 
    Much faster implementation for aligning chunks that uses 
    persistent_popen_align3() function.
    """
    # CHECK: data are already chunked, read in the whole thing. bail if no data
    clusts = []
    try:
        with open(handle, 'rt') as infile:
            clusts = infile.read().split("//\n//\n")
            # remove any empty spots
            clusts = [i for i in clusts if i]
            # Skip entirely empty chunks; return 0 if no clusters in file
            # Allows some chunks to be empty without raising an error.
            if not clusts:
                return 0

    # return 0 if file not read for some reason...
    except IOError:
        return 0

    # count discarded clusters for printing to stats later
    highindels = 0
    nwdups = 0
    nwodups = 0

    # iterate over clusters sending each to muscle, splits and aligns pairs
    aligned = persistent_popen_align3(clusts, 200, is_gbs)

    # store good alignments to be written to file
    refined = []

    # filter and trim alignments
    for clust in aligned:
        # check for too many internal indels
        if not aligned_indel_filter(clust, max_internal_indels):
            refined.append(clust)
        else:
            highindels += 1

    # declone reads based on i5 tags in the header
    if declone:
        drefined, nwdups, nwodups = declone_clusters(refined)

    # write to file after
    if refined:
        outhandle = handle.rsplit(".", 1)[0] + ".aligned"
        with open(outhandle, 'wb') as outfile:
            try:
                outfile.write("\n//\n//\n".join(refined) + "\n")
            except TypeError:
                outfile.write(("\n//\n//\n".join(refined) + "\n").encode())

    # return nfiltered by indels, nreads in clusters, nreads after deduping
    return highindels


def persistent_popen_align3(clusts, maxseqs=200, is_gbs=False):
    """
    keeps a persistent bash shell open and feeds it muscle alignments
    """
    # create a separate shell for running muscle in, this is much faster
    # than spawning a separate subprocess for each muscle call
    proc = subprocess.Popen(
        ["bash"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    )

    # iterate over clusters in this file until finished
    aligned = []
    for clust in clusts:

        # new alignment string for read1s and read2s
        align1 = []
        align2 = []

        # don't bother aligning if only one seq
        if clust.count(">") == 1:
            aligned.append(clust.replace(">", "").strip())
        else:

            # do we need to split the alignment? (is there a PE insert?)
            try:
                # make into list (only read maxseqs lines, 2X cuz names)
                lclust = clust.split()[:maxseqs * 2]

                # try to split cluster list at nnnn separator for each read
                lclust1 = list(chain(*zip(
                    lclust[::2], [i.split("nnnn")[0] for i in lclust[1::2]])))
                lclust2 = list(chain(*zip(
                    lclust[::2], [i.split("nnnn")[1] for i in lclust[1::2]])))

                # put back into strings
                clust1 = "\n".join(lclust1)
                clust2 = "\n".join(lclust2)

                # Align the first reads.
                # The muscle command with alignment as stdin and // as split
                cmd1 = ("echo -e '{}' | {} -quiet -in - ; echo {}"
                        .format(clust1, BIN_MUSCLE, "//\n"))

                # send cmd1 to the bash shell
                proc.stdin.write(cmd1.encode())

                # read the stdout by line until splitter is reached
                # meaning that the alignment is finished.
                for line in iter(proc.stdout.readline, b'//\n'):
                    align1.append(line.decode())

                # Align the second reads.
                # The muscle command with alignment as stdin and // as split
                cmd2 = ("echo -e '{}' | {} -quiet -in - ; echo {}"
                        .format(clust2, BIN_MUSCLE, "//\n"))

                # send cmd2 to the bash shell
                proc.stdin.write(cmd2.encode())

                # read the stdout by line until splitter is reached
                # meaning that the alignment is finished.
                for line in iter(proc.stdout.readline, b'//\n'):
                    align2.append(line.decode())

                # join up aligned read1 and read2 and ensure names order match
                lines1 = "".join(align1)[1:].split("\n>")
                lines2 = "".join(align2)[1:].split("\n>")
                dalign1 = dict([i.split("\n", 1) for i in lines1])
                dalign2 = dict([i.split("\n", 1) for i in lines2])

                # sort the first reads
                keys = list(dalign1.keys())
                seed = [i for i in keys if i[-1] == "*"][0]
                keys.pop(keys.index(seed))
                order = [seed] + sorted(
                    keys, 
                    key=lambda x: int(x.split("=")[-1].split("\n")[0][:-2]),
                    reverse=True,
                )

                # combine in order
                alignpe = []                
                for key in order:
                    alignpe.append("\n".join([
                        key, 
                        dalign1[key].replace("\n", "") + "nnnn" + \
                        dalign2[key].replace("\n", "")]))

                # append aligned cluster string
                aligned.append("\n".join(alignpe).strip())

            # Malformed clust. Dictionary creation with only 1 element 
            except ValueError:
                logger.warning(
                    "Bad PE cluster - {}\nla1 - {}\nla2 - {}"
                    .format(clust, lines1, lines2)
                )

            ## Either reads are SE, or at least some pairs are merged.
            except IndexError:

                # limit the number of input seqs
                # use lclust already built before checking pairs
                lclust = "\n".join(clust.split()[:maxseqs * 2])

                # the muscle command with alignment as stdin and // as splitter
                cmd = ("echo -e '{}' | {} -quiet -in - ; echo {}"
                       .format(lclust, BIN_MUSCLE, "//\n"))

                ## send cmd to the bash shell (TODO: PIPE could overflow here!)
                proc.stdin.write(cmd.encode())

                ## read the stdout by line until // is reached. This BLOCKS.
                for line in iter(proc.stdout.readline, b'//\n'):
                    align1.append(line.decode())

                ## remove '>' from names, and '\n' from inside long seqs                
                lines = "".join(align1)[1:].split("\n>")

                # find seed of the cluster and put it on top.
                seed = [i for i in lines if i.split('\n')[0][-1] == "*"][0]
                lines.pop(lines.index(seed))
                lines = [seed] + sorted(
                    lines, 
                    key=lambda x: int(x.split("=")[-1].split("\n")[0][:-2]),
                    reverse=True,
                )

                # format remove extra newlines from muscle
                aaa = [i.split("\n", 1) for i in lines]
                align1 = [
                    i[0] + '\n' + "".join([j.replace("\n", "")
                    for j in i[1:]]) for i in aaa
                ]

                # trim edges in sloppy gbs/ezrad data.
                # Maybe relevant to other types too...
                if is_gbs:
                    align1 = gbs_trim(align1)

                ## append to aligned
                aligned.append("\n".join(align1))

    # cleanup
    proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
    proc.stdin.close()
    proc.wait()

    ## return the aligned clusters
    return aligned


def declone_clusters(aligned):
    """
    Decloning clusters based on PCR duplicate tags.
    """
    # store new decloned sequence as list of strings
    decloned = []
    nwdups = 0
    nwodups = 0

    # iterate one locus at a time
    for loc in aligned:

        # the reads are ordered by depth, parse in order
        headers = loc.split("\n")[::2]
        seqs = loc.split("\n")[1::2]

        # parse headers
        bits = [i.split(";") for i in headers]
        names = [i[0] for i in bits]
        tags = [i[1] for i in bits]
        sizes = [int(i[2][5:]) for i in bits]

        # keep track of stats
        nwdups += sum(sizes)
        nwodups += len(set(tags))

        # if not repeated tags then skip to next locus
        if len(set(tags)) == len(tags):
            decloned.append(loc)
            continue

        # else: declone-------------------------------
        updated = {}

        # dict mapping {tag: 12} sum of all molecules with tag
        tagsize = {i: 0 for i in set(tags)}
        for idx in range(len(names)):
            tag = tags[idx]
            size = sizes[idx]
            tagsize[tag] += size

        # dict mapping {tag: [(10, seq), (1, seq), (1, seq)]} count,seq tuples
        tags2seqs = {}
        for idx in range(len(names)):
            if tag in tags2seqs:
                tags2seqs[tag].append((sizes[idx], seqs[idx]))
            else:
                tags2seqs[tag] = [(sizes[idx], seqs[idx])]

        # if one tag is most abundant then only keep it with its new depth.
        for tag, dtups in tags2seqs.items():

            # is one seq most abundant?
            depths = [i[0] for i in dtups]
            if depths[0] > depths[1]:
                updated[tag] = dtups[0][1]

            # most abundant tag is equal with one or more others
            else:
                # simple solution for now...
                updated[tag] = dtups[0][1]
                # get all equally abundant tags
                # allseqs = [i[1] for i in dtups if i[0] == dtups[0][0]]
                # mask conflicts and merge N-

        # write back into original order 
        seen = set()
        loc = []
        for idx, _ in enumerate(names):
            if tags[idx] not in seen:
                header = "{};{};size={};".format(names[idx], tags[idx], tagsize[tags[idx]])
                loc.append(header)
                loc.append(seqs[idx])
            seen.add(tags[idx])

        # join into a string locus
        decloned.append("\n".join(loc))
    return decloned, nwdups, nwodups


def gbs_trim(align1):
    """
    No reads can go past the left of the seed, or right of the least extended
    reverse complement match. Example below. m is a match. u is an area where 
    lots of mismatches typically occur. The cut sites are shown.

    Original locus*
    Seed           TGCAG************************************-----------------------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm-----------------------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm-----------------------------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm------------------------
    Revcomp-match  ------------------------mmmmmmmmmmmmmmmmmmmmmmmmmmmCTGCAuuuuuuuu
    Revcomp-match  ---------------mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmCTGCAuuuuuuuuuuuuuu
    Revcomp-match  --------------------------------mmmmmmmmmmmmmmmmmmmmmmmmmmmCTGCA
    Revcomp-match  ------------------------mmmmmmmmmmmmmmmmmmmmmmmmmmmCTGCAuuuuuuuu

    Trimmed locus*
    Seed           TGCAG************************************---------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm---------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm---------------
    Forward-match  TGCAGmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm----------
    Revcomp-match  ------------------------mmmmmmmmmmmmmmmmmmmmmmmmmm
    Revcomp-match  ---------------mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmCTGCA
    Revcomp-match  --------------------------------mmmmmmmmmmmmmmmmmm
    Revcomp-match  ------------------------mmmmmmmmmmmmmmmmmmmmmmmmmm
    """
    # get the left and rigth most occurrences of the cut site
    leftmost = rightmost = None

    # create dict mapping {seqname: seq}
    ddd = {k: v for k, v in [j.rsplit("\n", 1) for j in align1]}

    # get the seed sequence which contains "*" in header
    seed = [i for i in ddd.keys() if i.rsplit(";")[-1][0] == "*"][0]

    # position of the leftmost sequence that is not a "-" char.
    leftmost = [i != "-" for i in ddd[seed]].index(True)

    # which sequences are revcomp matched to the seed
    revs = [i for i in ddd.keys() if i.rsplit(";")[-1][0] == "-"]

    # ...
    if revs:
        subright = max([
            [i != "-" for i in seq[::-1]].index(True) 
            for seq in [ddd[i] for i in revs]
        ])
    else:
        subright = 0
    rightmost = len(ddd[seed]) - subright

    # if locus got clobbered then print place-holder NNN
    names, seqs = zip(*[i.rsplit("\n", 1) for i in align1])
    if rightmost > leftmost:
        newalign1 = [n + "\n" + i[leftmost:rightmost] 
                     for i, n in zip(seqs, names)]
    else:
        newalign1 = [n + "\nNNN" for i, n in zip(seqs, names)]
    return newalign1


def aligned_indel_filter(clust, max_internal_indels):
    """ 
    Checks for too many internal indels in muscle aligned clusters 
    """
    # make into list
    lclust = clust.split()

    # paired or not
    try:
        seq1 = [i.split("nnnn")[0] for i in lclust[1::2]]
        seq2 = [i.split("nnnn")[1] for i in lclust[1::2]]
        intindels1 = [i.rstrip("-").lstrip("-").count("-") for i in seq1]
        intindels2 = [i.rstrip("-").lstrip("-").count("-") for i in seq2]
        intindels = intindels1 + intindels2
        if max(intindels) > max_internal_indels:
            return 1
    except IndexError:
        seq1 = lclust[1::2]
        intindels = [i.rstrip("-").lstrip("-").count("-") for i in seq1]
        if max(intindels) > max_internal_indels:
            return 1     
    return 0


def reconcat(data, sample):
    """ 
    takes aligned chunks (usually 10) and concatenates them
    """
    # get chunks
    chunks = glob.glob(
        os.path.join(data.tmpdir, f"{sample.name}_chunk_[0-9].aligned"))       

    # sort by chunk number, cuts off last 8 =(aligned)
    chunks.sort(key=lambda x: int(x.rsplit("_", 1)[-1][:-8]))

    # concatenate finished reads
    clustfile = os.path.join(
        data.stepdir, f"{sample.name}.clusters.gz")
    with gzip.open(clustfile, 'wt') as out:
        for fname in chunks:
            with open(fname) as infile:
                dat = infile.read().strip() + "\n//\n//\n"
                out.write(dat)


def set_sample_stats(data, sample):
    """Sets step3 stats to Samples from clusters files.

    This is used for both denovo and reference assemblies.
    Iterates over clustS files to count data, returns maxlen and 
    depths arrays for each sample.
    """
    clustfile = os.path.join(
        data.stepdir, f"{sample.name}.clusters.gz")

    # get new clustered loci
    with gzip.open(clustfile, 'rt') as infile:
        pairdealer = zip(*[infile] * 2)

        # storage
        counts = []
        depths = []
        maxlen = []

        # start with cluster 0
        tdepth = 0
        tlen = 0
        tcount = 0

        # iterate until empty
        while 1:
            try:
                name, seq = next(pairdealer)
            except StopIteration:
                break

            # if at the end of a cluster
            if name == "//\n":
                depths.append(tdepth)
                maxlen.append(tlen)
                counts.append(tcount)
                tlen = 0
                tdepth = 0
                tcount = 0
            else:
                tdepth += int(name.strip().split("=")[-1][:-2])
                tlen = len(seq)
                tcount += 1
    slens, depths, counts = np.array(maxlen), np.array(depths), np.array(counts)

    # sample does not advance state
    if not depths.size:
        sample.stats_s3.clusters_total = 0
        sample.stats_s3.clusters_hidepth = 0
        return sample

    sample.state = 3
    sample.files.clusters = clustfile
    sample.stats_s3 = Stats3()

    # store depth settings at this time.
    sample.stats_s3.min_depth_maj_during_step3 = data.params.min_depth_majrule
    sample.stats_s3.min_depth_stat_during_step3 = data.params.min_depth_statistical

    # depth masks
    maj_mask = depths >= data.params.min_depth_majrule
    hid_mask = depths >= data.params.min_depth_statistical

    # store stats
    hilens = slens[hid_mask]
    sample.stats_s3.max_hidepth_cluster_length = int(hilens.max())
    sample.stats_s3.mean_hidepth_cluster_length = float(hilens.mean())
    sample.stats_s3.std_hidepth_cluster_length = float(hilens.std())
    sample.stats_s3.clusters_total = int(depths.shape[0])
    sample.stats_s3.clusters_hidepth = int(depths[maj_mask].shape[0])
    if data.hackers.declone_PCR_duplicates:
        sample.stats_s3.deduplicated_reads = int(sum(depths) - sum(counts))
        sample.stats_s3.deduplicated_reads = float(
            float(sum(depths) - sum(counts)) / sum(depths)
        )
    # store depths histogram as a dict. Limit to first 25 bins
    bars, _ = np.histogram(depths, bins=range(1, 26))
    sample.stats_s3.depths_histogram = [int(i) for i in bars]
    sample.stats_s3.mean_depth_total = float(depths.mean())
    sample.stats_s3.mean_depth_mj = float(depths[maj_mask].mean())
    sample.stats_s3.mean_depth_stat = float(depths[hid_mask].mean())
    sample.stats_s3.std_depth_total = float(depths.std())
    sample.stats_s3.std_depth_mj = float(depths[maj_mask].std())
    sample.stats_s3.std_depth_stat = float(depths[hid_mask].std())

    # store nreads mapped to the reference_filter...
    if data.params.assembly_method == "reference":

        # paired-end reads map to 1/2 as many loci (2 reads per locus)
        mapped = int(sum(depths))
        if data.is_pair:
            mapped *= 2
        sample.stats_s3.reads_mapped_to_ref = mapped
        # hack, this should not be possible to be >1
        sample.stats_s3.reads_mapped_to_ref_prop = min(round(float(
            sample.stats_s3.reads_mapped_to_ref / 
            sample.stats_s2.reads_passed_filter
        ), 2), 1.0)
    return sample
