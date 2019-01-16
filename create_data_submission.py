import os
import itertools
import subprocess
from dbApp.models import symportal_framework, data_set, reference_sequence, data_set_sample_sequence, analysis_type, \
    analysis_group, data_set_sample, data_analysis, clade_collection, clade_collection_type
from multiprocessing import Queue, Process, Manager
from django import db
import pickle
import csv
import numpy as np
from collections import defaultdict
import shutil
import re
import json
import glob
from datetime import datetime
import sys
import pandas as pd
from output import div_output_pre_analysis_new_meta_and_new_dss_structure
from general import *
from distance import generate_within_clade_UniFrac_distances_samples, generate_within_clade_BrayCurtis_distances_samples
from plotting import generate_stacked_bar_data_submission, plot_between_sample_distance_scatter


def log_qc_error_and_continue(datasetsampleinstanceinq, samplename, errorreason):
    print('Error in processing sample: {}'.format(samplename))
    datasetsampleinstanceinq.finalUniqueSeqNum = 0
    datasetsampleinstanceinq.finalTotSeqNum = 0
    datasetsampleinstanceinq.initialProcessingComplete = True
    datasetsampleinstanceinq.errorInProcessing = True
    datasetsampleinstanceinq.errorReason = errorreason
    datasetsampleinstanceinq.save()

    return


def worker_initial_mothur(input_q, error_sample_list, wkd, data_sub_id, debug):
    # I am going to make it so that we do all of the screening of the below evalue cutoff seqs
    # inline with the submission rather than spitting the sequences out at the end and having to re-do submissions.
    # We will constantly update the symClade.fa and make a backup at each stage we update it. This can simply be
    # a time stamped fasta.
    # we will need to split up this worker function into several stages. The first will be the 'initial mothur'

    '''This worker performs the pre-MED processing'''
    data_sub_in_q = data_set.objects.get(id=data_sub_id)
    for contigPair in iter(input_q.get, 'STOP'):
        sample_name = contigPair.split('\t')[0].replace('[dS]', '-')

        data_set_sample_instance_in_q = data_set_sample.objects.get(name=sample_name, dataSubmissionFrom=data_sub_in_q)
        # Only process samples that have not already had this done.
        # This should be handy in the event of crashed midprocessing
        if not data_set_sample_instance_in_q.initialProcessingComplete:
            # NB We will always crop with the SYMVAR primers as they produce the shortest product
            primer_fwd_seq = 'GAATTGCAGAACTCCGTGAACC'  # Written 5'-->3'
            primer_rev_seq = 'CGGGTTCWCTTGTYTGACTTCATGC'  # Written 5'-->3'

            oligo_file = [
                r'#SYM_VAR_5.8S2',
                'forward\t{0}'.format(primer_fwd_seq),
                r'#SYM_VAR_REV',
                'reverse\t{0}'.format(primer_rev_seq)
            ]

            # Initial Mothur QC, making contigs, screening for ambiguous calls and homopolymers
            # Uniqueing, discarding <2 abundance seqs, removing primers and adapters
            sys.stdout.write('{0}: QC started\n'.format(sample_name))
            current_directory = r'{0}/{1}/'.format(wkd, sample_name)

            # Make the sample by sample directory that we will be working in
            # this will be inside the wkd directory (the temp data directory for the data_set submission)
            os.makedirs(current_directory, exist_ok=True)

            # We also need to make the same sample by sample directories for the pre MED sequence dump
            os.makedirs(current_directory.replace('tempData', 'pre_MED_seqs'), exist_ok=True)

            stability_file = [contigPair]
            stability_file_name = r'{0}{1}'.format(sample_name, 'stability.files')
            root_name = r'{0}stability'.format(sample_name)
            stability_file_path = r'{0}{1}'.format(current_directory, stability_file_name)

            # write out the stability file. This will be a single pair of contigs with a sample name
            writeListToDestination(stability_file_path, stability_file)

            # Write oligos file to directory. This file contains the primer sequences used for PCR cropping
            writeListToDestination('{0}{1}'.format(current_directory, 'primers.oligos'), oligo_file)

            # NB mothur is working very strangely with the python subprocess command. For some
            # reason it is adding in an extra 'mothur' before the filename in the input directory
            # As such we will have to enter all of the paths to files absolutely

            # The mothur batch file that will be run by mothur.
            mothur_batch_file = [
                r'set.dir(input={0})'.format(current_directory),
                r'set.dir(output={0})'.format(current_directory),
                r'make.contigs(file={}{})'.format(current_directory, stability_file_name),
                r'summary.seqs(fasta={}{}.trim.contigs.fasta)'.format(current_directory, root_name),
                r'screen.seqs(fasta={0}{1}.trim.contigs.fasta, group={0}{1}.contigs.groups, '
                r'maxambig=0, maxhomop=5)'.format(current_directory, root_name),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.fasta)'.format(current_directory, root_name),
                r'unique.seqs(fasta={0}{1}.trim.contigs.good.fasta)'.format(current_directory, root_name),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.fasta, '
                r'name={0}{1}.trim.contigs.good.names)'.format(current_directory, root_name),
                r'split.abund(cutoff=2, fasta={0}{1}.trim.contigs.good.unique.fasta, '
                r'name={0}{1}.trim.contigs.good.names, group={0}{1}.contigs.good.groups)'.format(current_directory, root_name),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta, '
                r'name={0}{1}.trim.contigs.good.abund.names)'.format(current_directory, root_name),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.rare.fasta, '
                r'name={0}{1}.trim.contigs.good.rare.names)'.format(current_directory, root_name),
                r'pcr.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta, group={0}{1}.contigs.good.abund.groups, '
                r'name={0}{1}.trim.contigs.good.abund.names, '
                r'oligos={0}primers.oligos, pdiffs=2, rdiffs=2)'.format(current_directory, root_name)
            ]

            # Write out the batch file
            mothur_batch_file_path = r'{0}{1}{2}'.format(current_directory, 'mothur_batch_file', sample_name)
            writeListToDestination(mothur_batch_file_path, mothur_batch_file)

            error = False
            # NB the mothur return code doesn't seem to work. We just get None type.
            # apparently they have fixed this in the newest mothur but we have not upgraded to that yet.
            # so for the time being we will check for error by hand in the stdout.
            with subprocess.Popen(['mothur', '{0}'.format(mothur_batch_file_path)], stdout=subprocess.PIPE, bufsize=1,
                                  universal_newlines=True) as p:
                # Here look for the specific blank fasta name warning (which should be interpreted as an error)
                # and any other error that may be arising
                # if found, log error.
                for line in p.stdout:
                    if debug:
                        print(line)
                    if '[WARNING]: Blank fasta name, ignoring read.' in line:
                        p.terminate()
                        errorReason = 'Blank fasta name'
                        log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, errorReason)
                        error = True
                        error_sample_list.append(sample_name)
                        break
                    if 'ERROR' in line:
                        p.terminate()
                        errorReason = 'error in inital QC'
                        log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, errorReason)
                        error = True
                        error_sample_list.append(sample_name)
                        break

            if error:
                continue

            # Here check the outputted files to see if they are reverse complement
            # or not by running the pcr.seqs and checking the results
            # Check to see if there are sequences in the PCR output file
            last_summary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.fasta'.format(current_directory, root_name))

            # If this file is empty
            #  Then these sequences may well be reverse complement so we need to try to rev first
            if len(last_summary) == 0:

                # RC batch file
                mothur_batch_reverse = [
                    r'reverse.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta)'.format(current_directory, root_name),
                    r'pcr.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.rc.fasta, '
                    r'group={0}{1}.contigs.good.abund.groups, name={0}{1}.trim.contigs.good.abund.names, '
                    r'oligos={0}primers.oligos, pdiffs=2, rdiffs=2)'.format(current_directory, root_name)
                ]
                mothur_batch_file_path = r'{0}{1}{2}'.format(current_directory, 'mothur_batch_file', sample_name)
                # write out RC batch file
                writeListToDestination(mothur_batch_file_path, mothur_batch_reverse)

                if not debug:
                    completed_process = subprocess.run(
                        ['mothur', r'{0}'.format(mothur_batch_file_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    # At this point the sequences will be reversed and they will have been renamed so we
                    # can just change the name of the .rc file to the orignal .fasta file that we inputted with
                    # This way we don't need to change the rest of the mothur pipe.
                    subprocess.run(
                        [r'mv', r'{0}{1}.trim.contigs.good.unique.abund.rc.pcr.fasta'.format(current_directory, root_name),
                         r'{0}{1}.trim.contigs.good.unique.abund.pcr.fasta'.format(current_directory, root_name)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                elif debug:
                    completed_process = subprocess.run(
                        ['mothur', r'{0}'.format(mothur_batch_file_path)])
                    subprocess.run(
                        [r'mv', r'{0}{1}.trim.contigs.good.unique.abund.rc.pcr.fasta'.format(current_directory, root_name),
                         r'{0}{1}.trim.contigs.good.unique.abund.pcr.fasta'.format(current_directory, root_name)])

            # Check again to see if the RC has fixed the problem of having an empty fasta
            # If this file is still empty, then the problem was not solved by reverse complementing
            last_summary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.fasta'.format(current_directory, root_name))

            if len(last_summary) == 0:
                errorReason = 'error in inital QC'
                log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, errorReason)
                error_sample_list.append(sample_name)
                continue

            # after having completed the RC checks redo the unique.
            mothur_batch_file_cont = [
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.fasta, '
                r'name={0}{1}.trim.contigs.good.abund.pcr.names)'.format(current_directory, root_name),
                r'unique.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.fasta, '
                r'name={0}{1}.trim.contigs.good.abund.pcr.names)'.format(current_directory, root_name),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.unique.fasta, '
                r'name={0}{1}.trim.contigs.good.unique.abund.pcr.names)'.format(current_directory, root_name)
            ]

            mothur_batch_file_path = r'{0}{1}{2}'.format(current_directory, 'mothur_batch_file', sample_name)
            writeListToDestination(mothur_batch_file_path, mothur_batch_file_cont)

            completed_process = subprocess.run(
                ['mothur', r'{0}'.format(mothur_batch_file_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if completed_process.returncode == 1 or 'ERROR' in completed_process.stdout.decode('utf-8'):
                if debug:
                    print(completed_process.stdout.decode('utf-8'))
                errorReason = 'error in inital QC'
                log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, errorReason)
                error_sample_list.append(sample_name)
                continue

            # Check to see if there are sequences in the PCR output file
            try:
                last_summary = readDefinedFileToList(
                    '{}{}.trim.contigs.good.unique.abund.pcr.unique.fasta'.format(current_directory, root_name))
                if len(last_summary) == 0:  # If this file is empty
                    errorReason = 'error in inital QC'
                    log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, errorReason)
                    error_sample_list.append(sample_name)
                    continue
            except FileNotFoundError:  # If there is no file then we can assume sample has a problem
                log_qc_error_and_continue(data_set_sample_instance_in_q, sample_name, 'generic_error')
                continue

            # Get number of sequences after make.contig
            last_summary = readDefinedFileToList('{}{}.trim.contigs.summary'.format(current_directory, root_name))
            number_of_seqs_contig_absolute = len(last_summary) - 1
            data_set_sample_instance_in_q.initialTotSeqNum = number_of_seqs_contig_absolute
            sys.stdout.write(
                '{}: data_set_sample_instance_in_q.initialTotSeqNum = {}\n'.format(sample_name,
                                                                              number_of_seqs_contig_absolute))

            # Get number of sequences after unique
            last_summary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(current_directory, root_name))
            number_of_seqs_contig_unique = len(last_summary) - 1
            data_set_sample_instance_in_q.initialUniqueSeqNum = number_of_seqs_contig_unique
            sys.stdout.write(
                '{}: data_set_sample_instance_in_q.initialUniqueSeqNum = {}\n'.format(sample_name,
                                                                                 number_of_seqs_contig_unique))

            # Get absolute number of sequences after after sequence QC
            last_summary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(current_directory, root_name))
            absolute_count = 0
            for line in last_summary[1:]:
                absolute_count += int(line.split('\t')[6])
            data_set_sample_instance_in_q.post_seq_qc_absolute_num_seqs = absolute_count
            data_set_sample_instance_in_q.save()
            sys.stdout.write('{}: data_set_sample_instance_in_q.post_seq_qc_absolute_num_seqs = {}\n'.format(sample_name,
                                                                                                        absolute_count))

            sys.stdout.write('{}: Initial mothur complete\n'.format(sample_name))
            # Each sampleDataDir should contain a set of .fasta, .name and .group
            # files that we can use to do local blasts with

    return


def perform_med(wkd, uid, num_proc, debug):
    # Create mothur batch for each .fasta .name pair to be deuniqued
    # Put in directory list, run via multiprocessing
    samples_collection = data_set_sample.objects.filter(dataSubmissionFrom=data_set.objects.get(id=uid))
    mothur_batch_file_path_list = []
    for dataSetSampleInstance in samples_collection:  # For each samples directory
        sample_name = dataSetSampleInstance.name
        full_path = '{}/{}'.format(wkd, sample_name)

        # http: // stackoverflow.com / questions / 3207219 / how - to - list - all - files - of - a - directory
        list_of_dirs = []
        for (dirpath, dirnames, filenames) in os.walk(full_path):
            list_of_dirs.extend(dirnames)
            break
        for directory in list_of_dirs:  # for each cladal directory
            fasta_file_path = ''
            name_file_path = ''
            path_to_dir = '{0}/{1}'.format(full_path, directory)
            clade_name = directory
            # For each of the files in each of the Cladal directories
            list_of_files = []
            for (dirpath, dirnames, filenames) in os.walk(path_to_dir):
                list_of_files.extend(filenames)
                break

            for files in list_of_files:
                if '.fasta' in files and '.redundant' not in files:
                    fasta_file_path = '{0}/{1}'.format(path_to_dir, files)
                elif '.names' in files:
                    name_file_path = '{0}/{1}'.format(path_to_dir, files)

            # Build a quick mothur_batch_file
            mothur_batch_file = [
                r'set.dir(input={0}/)'.format(path_to_dir),
                r'set.dir(output={0}/)'.format(path_to_dir),
                r'deunique.seqs(fasta={0}, name={1})'.format(fasta_file_path, name_file_path)
            ]
            mothur_batch_file_path = '{0}/{1}'.format(path_to_dir, '{0}.{1}.{2}'.format(sample_name, clade_name, 'mothur_batch_file'))
            writeListToDestination(mothur_batch_file_path, mothur_batch_file)
            mothur_batch_file_path_list.append(mothur_batch_file_path)

    # Create the queues that will hold the mothur_batch_file paths
    task_queue = Queue()
    done_queue = Queue()

    for mothur_batch_file_path in mothur_batch_file_path_list:
        task_queue.put(mothur_batch_file_path)

    for n in range(num_proc):
        task_queue.put('STOP')

    all_processes = []

    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    for n in range(num_proc):
        p = Process(target=deunique_worker, args=(task_queue, done_queue, debug))
        all_processes.append(p)
        p.start()

    # Collect the list of deuniqued directories to use for MED analyses
    list_of_deuniqued_fasta_paths = []
    for i in range(len(mothur_batch_file_path_list)):
        list_of_deuniqued_fasta_paths.append(done_queue.get())

    for p in all_processes:
        p.join()

    return list_of_deuniqued_fasta_paths


def deunique_worker(input, output, debug):
    # This currently works through a list of paths to batch files to be uniques.
    # But at each of these locations once the modified deuniqued file has been written we can then perform the MED
    # analysis on the file in each of the directories.
    # We also want to be able to read in the results of the MED but we will not be able to do that as MP so we
    # will have to save the list of directories and go through them one by one to create the sequences

    for mothur_batch_file_path in iter(input.get, 'STOP'):

        cwd = os.path.dirname(mothur_batch_file_path)
        sample_name = cwd.split('/')[-2]

        sys.stdout.write('{}: deuniqueing QCed seqs\n'.format(sample_name))
        found = True

        # Run the dunique
        if not debug:
            completed_process = subprocess.run(['mothur', r'{0}'.format(mothur_batch_file_path)], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
        elif debug:
            subprocess.run(['mothur', r'{0}'.format(mothur_batch_file_path)])
        # Modify the deuniqued fasta to append sample name to all of the sequences
        # Get list of files in directory
        deuniqued_fasta = []

        # Replace '_' in name as MED uses text up to first underscore as sample name
        # This shouldn't be necessary
        # sample_name = sample_name.replace('_', '-')
        list_of_files = []
        for (dirpath, dirnames, filenames) in os.walk(cwd):
            list_of_files.extend(filenames)
            break
        path_to_file = None
        for file in list_of_files:
            if '.redundant' in file:  # Then this is the deuniqued fasta
                path_to_file = '{0}/{1}'.format(cwd, file)

                break
        deuniqued_fasta = readDefinedFileToList(path_to_file)
        deuniqued_fasta = ['{0}{1}_{2}'.format(a[0], sample_name, a[1:].replace('_', '-')) if a[0] == '>' else a for a in
                          deuniqued_fasta]
        # write the modified deuniqued_fasta to list
        writeListToDestination(path_to_file, deuniqued_fasta)
        if debug:
            if deuniqued_fasta:
                if len(deuniqued_fasta) < 100:
                    print('WARNING the dequniqed fasta for {} is less than {} lines'.format(sample_name,
                                                                                            len(deuniqued_fasta)))
            else:
                print('deuniqued fasta for {} is empty'.format(sample_name))
        # Put the path to the deuniqued fasta into the output list for use in MED analyses
        output.put('{}/{}/'.format(os.path.dirname(path_to_file), 'MEDOUT'))

        # The fasta that we want to pad and MED is the 'file'
        sys.stdout.write('{}: padding alignment\n'.format(sample_name))
        path_to_med_padding = os.path.join(os.path.dirname(__file__), 'lib/med_decompose/o_pad_with_gaps.py')

        subprocess.run([path_to_med_padding, r'{}'.format(path_to_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Now run MED
        list_of_files = []
        for (dirpath, dirnames, filenames) in os.walk(cwd):
            list_of_files.extend(filenames)
            break
        for file in list_of_files:
            if 'PADDED' in file:
                path_to_file = '{0}/{1}'.format(cwd, file)
                break
        med_out_dir = '{}/{}/'.format(cwd, 'MEDOUT')
        os.makedirs(med_out_dir, exist_ok=True)
        sys.stdout.write('{}: running MED\n'.format(sample_name))
        # Here we need to make sure that the M value is defined dynamically
        # the M value is a cutoff that looks at the abundance of the most abundant unique sequence in a node
        # if the abundance is lower than M then the node is discarded
        # we have been working recently with an M that equivaltes to 0.4% of 0.004. This was
        # calculated when working with a modelling project where I was subsampling to 1000 sequences. In this
        # scenario the M was set to 4.
        # We should also take care that M doesn't go below 4, so we should use a max choice for the M
        m_value = max(4, int(0.004 * (len(deuniqued_fasta) / 2)))
        path_to_med_decompose = os.path.join(os.path.dirname(__file__), 'lib/med_decompose/decompose.py')
        if not debug:
            subprocess.run(
                [path_to_med_decompose, '-M', str(m_value), '--skip-gexf-files', '--skip-gen-figures',
                 '--skip-gen-html', '--skip-check-input', '-o',
                 med_out_dir, path_to_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        elif debug:
            subprocess.run(
                [path_to_med_decompose, '-M', str(m_value), '--skip-gexf-files', '--skip-gen-figures',
                 '--skip-gen-html',
                 '--skip-check-input', '-o',
                 med_out_dir, path_to_file])
        sys.stdout.write('{}: MED complete\n'.format(sample_name))


def check_if_seq_in_q_had_ref_seq_match(seq_in_q, node_name, ref_seq_id_dict, node_to_ref_dict, ref_seq_id_name_dict):
    # seq_in_q = the MED node sequence in question
    # ref_seq_id_dict = dictionary of all current ref_sequences sequences (KEY) to their ID (VALUE).
    # We use this to look to see if there is an equivalent refSeq Sequence for the sequence in question
    # This take into account whether the seq_in_q could be a subset or super set of one of the
    # refSeq.sequences
    # Will return false if no refSeq match is found

    # first check to see if seq is found
    if seq_in_q in ref_seq_id_dict:  # Found actual seq in dict
        # assign the MED node name to the reference_sequence ID that it matches
        node_to_ref_dict[node_name] = ref_seq_id_dict[seq_in_q]
        sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(node_name,
                                                                                            ref_seq_id_name_dict[
                                                                                                ref_seq_id_dict[seq_in_q]]))
        return True
    elif 'A' + seq_in_q in ref_seq_id_dict:  # This was a seq shorter than refseq but we can associate it to this ref seq
        # assign the MED node name to the reference_sequence ID that it matches
        node_to_ref_dict[node_name] = ref_seq_id_dict['A' + seq_in_q]
        sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(node_name, ref_seq_id_name_dict[
            ref_seq_id_dict['A' + seq_in_q]]))
        return True
    else:  # This checks if either the seq in question is found in the sequence of a reference_sequence
        # or if the seq in question is bigger than a refseq sequence and is a super set of it
        # In either of these cases we should consider this a match and use the refseq matched to.
        # This might be very coputationally expensive but lets give it a go

        for ref_seq_key in ref_seq_id_dict.keys():
            if seq_in_q in ref_seq_key or ref_seq_key in seq_in_q:
                # Then this is a match
                node_to_ref_dict[node_name] = ref_seq_id_dict[ref_seq_key]
                sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(
                    node_name, ref_seq_id_name_dict[ref_seq_id_dict[ref_seq_key]]))
                return True
    return False


def create_data_set_sample_sequences_from_med_nodes(wkd, identification, med_dirs, debug):
    ''' Here we have modified the original method processMEDDataDirectCCDefinition'''
    # We are going to change this so that we go to each of the med_dirs, which represent the clades within samples
    # that have had MED analyses run in them and we are going to use the below code to populate sequences to
    # the CCs and samples

    # in check_if_seq_in_q_had_ref_seq_match method below we are currently doing lots of database look ups to
    # get the names of reference_sequecnes
    # this is likely quite expensive so I think it will be easier to make a dict for this purpose which is
    # reference_sequence.id (KEY) reference_sequence.name (VALUE)
    reference_sequence_id_to_name_dict = {refSeq.id: refSeq.name for refSeq in reference_sequence.objects.all()}

    # This is a dict of key = reference_sequence.sequence value = reference_sequence.id for all refseqs
    # currently held in the database
    # We will use this to see if the sequence in question has a match, or is found in (this is key
    # as some of the seqs are one bp smaller than the reference seqs) there reference sequences
    reference_sequence_sequence_to_id_dict = {refSeq.sequence: refSeq.id for refSeq in reference_sequence.objects.all()}

    clade_list = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
    for dir in med_dirs:  # For each of the directories where we did MED
        os.chdir(dir)

        # Get the sample
        sample_name = dir.split('/')[-4]

        # Get the clade
        clade = dir.split('/')[-3]

        sys.stdout.write('\n\nPopulating {} with clade {} sequences\n'.format(sample_name, clade))

        # Read in the node file
        try:
            node_file = readDefinedFileToList('NODE-REPRESENTATIVES.fasta')
        except FileNotFoundError:
            # if no node file found move on to the next directory
            if debug:
                print('Could not locate NODE-REP file for {}'.format(dir))
            continue
        node_file = [line.replace('-', '') if line[0] != '>' else line for line in node_file]

        # Create node_to_ref_dict that will be populated
        node_to_ref_dict = {}

        # ASSOCIATE MED NODES TO EXISITING REFSEQS OR CREATE NEW REFSEQS
        # Look up seq of each of the MED nodes with reference_sequence table
        # See if the seq in Q matches a reference_sequence, if so, associate
        if debug:
            if len(node_file) < 10:
                print('WARNING node file for {} is only {} lines'.format(dir, len(node_file)))
        list_of_ref_seqs = []
        for i in range(len(node_file)):
            # We were having a problem here where some of the seqs were 1bp shorter than the reference seqs
            # As such they werent matching to the refernceSequence object e.g. to C3 but when we do the
            # blast they come up with C3 as their clostest and perfect match
            # To fix this we will run check_if_seq_in_q_had_ref_seq_match

            if node_file[i][0] == '>':  # Then this is a def line
                sequence_in_q = node_file[i + 1]
                node_name_in_q = node_file[i][1:].split('|')[0]
                # If True then node name will already have been associated to node_to_ref_dict
                # and no need to do anything else
                found = check_if_seq_in_q_had_ref_seq_match(seq_in_q=sequence_in_q,
                                                    ref_seq_id_dict=reference_sequence_sequence_to_id_dict,
                                                    node_name=node_name_in_q,
                                                    node_to_ref_dict=node_to_ref_dict,
                                                    ref_seq_id_name_dict=reference_sequence_id_to_name_dict)

                if not found:
                    # If there is no current match for the MED node in our current reference_sequences
                    # create a new reference_sequence object and add this to the refSeqDict
                    # Then assign the MED node to this new reference_sequence using the node_to_ref_dict
                    new_reference_sequence = reference_sequence(clade=clade, sequence=sequence_in_q)
                    new_reference_sequence.save()
                    new_reference_sequence.name = str(new_reference_sequence.id)
                    new_reference_sequence.save()
                    list_of_ref_seqs.append(new_reference_sequence)
                    reference_sequence_sequence_to_id_dict[new_reference_sequence.sequence] = new_reference_sequence.id
                    node_to_ref_dict[node_name_in_q] = new_reference_sequence.id
                    reference_sequence_id_to_name_dict[new_reference_sequence.id] = new_reference_sequence.name

                    sys.stdout.write(
                        '\rAssigning MED node {} to new reference sequence {}'.format(node_file[i][1:].split('|')[0],
                                                                                      new_reference_sequence.name))
        ########################################################################################

        # Here we have a refSeq associated to each of the seqs found and we can now create
        # dataSetSampleSequences that have associated referenceSequences
        # So at this point we have a reference_sequence associated with each of the nodes
        # Now it is time to define clade collections
        # Open the MED node count table as list of lists
        count_array = []
        nodes = []
        samples = []
        # this creates count_array which is a 2D list
        with open('MATRIX-COUNT.txt') as f:
            reader = csv.reader(f, delimiter='\t')
            count_array = list(reader)
        # get Nodes from first list
        nodes = count_array[0][1:]
        # del nodes
        del count_array[0]
        # get samples from first item of each list
        # del samples to leave raw numerical
        for i in range(len(count_array)):
            samples.append(count_array[i][0])
            del count_array[i][0]
        # convert to np array
        count_array = np.array(count_array)
        count_array = count_array.astype(np.int)
        # for each node in each sample create data_set_sample_sequence with foreign key
        # to referenceSeq and data_set_sample
        # give it a foreign key to the reference Seq by looking up the seq in the dictionary
        # made earlier and using the value to search for the referenceSeq

        for i in range(len(samples)):  # For each sample # There should only be one sample

            data_set_sample_object = data_set_sample.objects.get(dataSubmissionFrom=data_set.objects.get(id=identification),
                                                                 name=samples[i])
            # Add the metadata to the data_set_sample
            data_set_sample_object.post_med_absolute += sum(count_array[i])
            data_set_sample_object.post_med_unique += len(count_array[i])
            data_set_sample_object.save()
            cladal_seq_abundance_counter = [int(a) for a in json.loads(data_set_sample_object.cladalSeqTotals)]

            # This is where we need to tackle the issue of making sure we keep track of sequences in samples that
            # were not above the 200 threshold to be made into cladeCollections
            # We will simply add a list to the sampleObject that will be a sequence total for each of the clades
            # in order of clade_list

            # Here we modify the cladalSeqTotals string of the sample object to add the sequence totals
            # for the given clade
            clade_index = clade_list.index(clade)
            temp_int = cladal_seq_abundance_counter[clade_index]
            temp_int += sum(count_array[i])
            cladal_seq_abundance_counter[clade_index] = temp_int
            data_set_sample_object.cladalSeqTotals = json.dumps([str(a) for a in cladal_seq_abundance_counter])
            data_set_sample_object.save()

            dss_list = []

            if sum(count_array[i]) > 200:
                sys.stdout.write(
                    '\n{} clade {} sequences in {}. Creating clade_collection object\n'.format(sum(count_array[i]),
                                                                                               sample_name, clade))
                newCC = clade_collection(clade=clade, dataSetSampleFrom=data_set_sample_object)
                newCC.save()
            else:
                sys.stdout.write(
                    '\n{} clade {} sequences in {}. Insufficient sequence to create a clade_collection object\n'.format(
                        sum(count_array[i]), clade, sample_name))

            # I want to address a problem we are having here. Now that we have thorough checks to
            # associate very similar sequences with indels by the primers to the same reference seq
            # it means that multiple sequences within the same sample can have the same referenceseqs
            # Due to the fact that we will in effect use the sequence of the reference seq rather
            # than the dsss seq, we should consolidate all dsss seqs with the same reference seq
            # so... we will create a counter that will keep track of the cumulative abundance
            # associated with each reference_sequence
            # and then create a dsss for each refSeq from this.
            refSeqAbundanceCounter = defaultdict(int)
            for j in range(len(nodes)):
                abundance = count_array[i][j]
                if abundance > 0:
                    refSeqAbundanceCounter[reference_sequence.objects.get(id=node_to_ref_dict[nodes[j]])] += abundance

            # > 200 associate a CC to the data_set_sample, else, don't
            # irrespective, associate a data_set_sample_sequences to the data_set_sample
            sys.stdout.write(
                '\nAssociating clade {} data_set_sample_sequences directly to data_set_sample {}\n'.format(clade,
                                                                                                           sample_name))
            if sum(count_array[i]) > 200:
                for refSeq in refSeqAbundanceCounter.keys():
                    dss = data_set_sample_sequence(referenceSequenceOf=refSeq,
                                                   cladeCollectionTwoFoundIn=newCC,
                                                   abundance=refSeqAbundanceCounter[refSeq],
                                                   data_set_sample_from=data_set_sample_object)
                    dss_list.append(dss)
                # Save all of the newly created dss
                data_set_sample_sequence.objects.bulk_create(dss_list)
                # Get the ids of each of the dss and add create a string of them and store it as cc.footPrint
                # This way we can quickly get the footprint of the CC.
                # Sadly we can't get eh IDs from the list so we will need to re-query
                # Instead we add the identification of each refseq in the refSeqAbundanceCounter.keys() list
                newCC.footPrint = ','.join([str(refSeq.id) for refSeq in refSeqAbundanceCounter.keys()])
                newCC.save()
            else:
                for refSeq in refSeqAbundanceCounter.keys():
                    dss = data_set_sample_sequence(referenceSequenceOf=refSeq,
                                                   abundance=refSeqAbundanceCounter[refSeq],
                                                   data_set_sample_from=data_set_sample_object)
                    dss_list.append(dss)
                # Save all of the newly created dss
                data_set_sample_sequence.objects.bulk_create(dss_list)

    return


def main(pathToInputFile, dSID, numProc, screen_sub_evalue=False,
         full_path_to_nt_database_directory='/home/humebc/phylogeneticSoftware/ncbi-blast-2.6.0+/ntdbdownload',
         data_sheet_path=None, noFig=False, noOrd=False, distance_method='braycurtis', debug=False):
    # UNZIP FILE, CREATE LIST OF SAMPLES AND WRITE stability.files FILE

    dataSubmissionInQ = data_set.objects.get(id=dSID)
    clade_list = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
    # we will create the output dire early on so that we can use it to write out the sample by sample
    # thrown away seqs.
    outputDir = os.path.join(os.path.dirname(__file__), 'outputs/data_set_submissions/{}'.format(dSID))
    os.makedirs(outputDir, exist_ok=True)
    if not dataSubmissionInQ.initialDataProcessed:

        # Identify sample names and generate new stability file, generate data_set_sample objects in bulk
        wkd, num_samples = generate_new_stability_file_and_data_set_sample_objects(clade_list, dSID, dataSubmissionInQ,
                                                                                   data_sheet_path, pathToInputFile)

        # PERFORM pre-MED QC
        if screen_sub_evalue:
            new_seqs_added_count, discarded_seqs_fasta = preMED_QC(dSID, dataSubmissionInQ, numProc, wkd,
                                                                   screen_sub_evalue, output_dir=outputDir, debug=debug)
        else:
            fasta_of_sig_sub_e_seqs, fasta_of_sig_sub_e_seqs_path, discarded_seqs_fasta = \
                preMED_QC(dSID, dataSubmissionInQ, numProc, wkd, screen_sub_evalue, output_dir=outputDir, debug=debug)

    # This function now performs the MEDs sample by sample, clade by clade.
    # The list of outputed paths lead to the MED directories where node info etc can be found
    sys.stdout.write('\n\nStarting MED analysis\n')
    med_dirs = perform_med(dataSubmissionInQ.workingDirectory, dataSubmissionInQ.id, numProc, debug)

    if debug:
        print('MED dirs:')
        for dir in med_dirs:
            print(dir)

    create_data_set_sample_sequences_from_med_nodes(dataSubmissionInQ.workingDirectory, dataSubmissionInQ.id, med_dirs,
                                                    debug)

    # dataSubmissionInQ.dataProcessed = True
    dataSubmissionInQ.currentlyBeingProcessed = False
    dataSubmissionInQ.save()

    # WRITE OUT REPORT OF HOW MANY SAMPLES WERE SUCCESSFULLY PROCESSED
    processed_samples_status(dataSubmissionInQ, pathToInputFile)

    # Here I also want to by default output a sequence drop that is a drop of the named sequences and their associated
    # sequences so that we mantain a link of the sequences to the names of the sequences
    perform_sequence_drop()

    # CLEAN UP tempData FOLDER #####
    # get rid of the entire temp folder rather than just the individual wkd for this data submission
    # just in case multiple
    print('Cleaning up temp folders')
    os.chdir(os.path.abspath(os.path.dirname(__file__)))
    dir_to_del = os.path.abspath('{}/tempData'.format(pathToInputFile))
    if os.path.exists(dir_to_del):
        shutil.rmtree(dir_to_del)

    # also make sure that we get rid of .logfile files in the symbiodiniumDB directory
    symbiodiniumdb_dir = os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')
    log_file_list = [f for f in os.listdir(symbiodiniumdb_dir) if f.endswith(".logfile")]
    for f in log_file_list:
        os.remove('{}/{}'.format(symbiodiniumdb_dir, f))

    # COUNT TABLE OUTPUT
    # We are going to make the sequence count table output as part of the dataSubmission
    sys.stdout.write('\nGenerating count tables\n')

    # the below method will create the tab delimited output table and print out the output file paths
    # it will also return these paths so that we can use them to grab the data for figure plotting
    output_path_list, date_time_str, num_samples = div_output_pre_analysis_new_meta_and_new_dss_structure(
        datasubstooutput=str(dSID),
        numProcessors=numProc,
        output_dir=outputDir, call_type='submission')

    # also write out the fasta of the sequences that were discarded
    discarded_seqs_fasta_path = '{}/discarded_seqs_{}.fasta'.format(outputDir, dSID)
    writeListToDestination(discarded_seqs_fasta_path, discarded_seqs_fasta)
    print('A fasta containing discarded sequences ({}) is output here:\n{}'.format(len(discarded_seqs_fasta) / 2,
                                                                                   discarded_seqs_fasta_path))

    ###################################
    # Stacked bar output fig
    # here we will create a stacked bar
    # I think it is easiest if we directly pass in the path of the above count table output
    if not noFig:
        if num_samples > 1000:
            print('Too many samples ({}) to generate plots'.format(num_samples))
        else:
            sys.stdout.write('\nGenerating sequence count table figures\n')
            for path in output_path_list:
                if 'relative' in path:
                    path_to_rel_abund_data = path

            svg_path, png_path = generate_stacked_bar_data_submission(path_to_rel_abund_data, outputDir,
                                                                      time_date_str=date_time_str)
            sys.stdout.write('\nFigure generation complete')
            sys.stdout.write('\nFigures output to:')
            sys.stdout.write('\n{}'.format(svg_path))
            sys.stdout.write('\n{}\n'.format(png_path))

    # between sample distances
    if not noOrd:
        print('Calculating between sample pairwise distances')
        if distance_method == 'unifrac':
            PCoA_paths_list = generate_within_clade_UniFrac_distances_samples(dataSubmission_str=dSID,
                                                                              num_processors=numProc,
                                                                              method='mothur', call_type='submission',
                                                                              date_time_string=date_time_str,
                                                                              output_dir=outputDir)
        elif distance_method == 'braycurtis':
            PCoA_paths_list = generate_within_clade_BrayCurtis_distances_samples(dataSubmission_str=dSID,
                                                                                 call_type='submission',
                                                                                 date_time_str=date_time_str,
                                                                                 output_dir=outputDir)
        # distance plotting
        if not noFig:
            if num_samples > 1000:
                print('Too many samples ({}) to generate plots'.format(num_samples))
            else:
                for pcoa_path in PCoA_paths_list:
                    if 'PCoA_coords' in pcoa_path:
                        # then this is a full path to one of the .csv files that contains the
                        # coordinates that we can plot we will get the output directory from the passed in pcoa_path
                        sys.stdout.write('\n\nGenerating between sample distance plot clade {}\n'.format(
                            os.path.dirname(pcoa_path).split('/')[-1]))
                        plot_between_sample_distance_scatter(csv_path=pcoa_path, date_time_str=date_time_str)
        ####################################
    #######################################

    # write out whether there were below e value sequences outputted.
    if screen_sub_evalue:
        sys.stdout.write('{} sequences were added to the symClade.fa database as part of this data submission\n'.format(
            new_seqs_added_count))
    else:
        if fasta_of_sig_sub_e_seqs:
            sys.stdout.write('{} distinct sequences from your submission were of questionable taxonomic origin.'
                             '\nSymPortal can\'t be sure that they are of Symbiodinium/Symbiodiniaceae origin '
                             'despite them showing some degree of similarity to the reference sequences.'
                             '\nA .fasta has been output which contains these sequences here:'
                             '\n{}\n'.format(len(fasta_of_sig_sub_e_seqs), fasta_of_sig_sub_e_seqs_path))
        else:
            sys.stdout.write('There were no sub evalue sequences returned - hooray!\n')

    print('data_set ID is: {}'.format(dataSubmissionInQ.id))
    return dataSubmissionInQ.id


def generate_and_write_below_evalue_fasta_for_screening(dSID, dataSubmissionInQ, e_value_multiP_dict, wkd, debug):
    # make fasta from the dict
    fasta_out = make_evalue_screening_fasta_no_clade(dSID, e_value_multiP_dict, wkd)
    # we need to know what clade each of the sequences are
    # fastest way to do this is likely to run another blast on the symbiodinium clade reference dict
    if fasta_out:
        fasta_out_with_clade = make_evalue_screening_fasta_with_clade(dataSubmissionInQ, fasta_out, wkd, debug)
        # this will return a new fasta containing only the sequences that were 'Symbiodinium' matches
        # we can then output this dictionary
        path_to_fasta_out_with_clade = wkd + '/below_e_cutoff_seqs_{}.fasta'.format(dSID)
        writeListToDestination(path_to_fasta_out_with_clade, fasta_out_with_clade)
        return fasta_out_with_clade, path_to_fasta_out_with_clade
    else:
        return fasta_out, None


def make_evalue_screening_fasta_with_clade(dataSubmissionInQ, fasta_out, wkd, debug):
    ncbircFile = []
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
    ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])
    # write the .ncbirc file that gives the location of the db
    writeListToDestination("{0}/.ncbirc".format(wkd), ncbircFile)
    blastOutputPath = r'{}/blast.out'.format(wkd)
    outputFmt = "6 qseqid sseqid staxids evalue"
    inputPath = r'{}/blastInputFasta.fa'.format(wkd)
    os.chdir(wkd)
    # Run local blast
    if not debug:
        completed_process = subprocess.run(
            ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db',
             dataSubmissionInQ.reference_fasta_database_used, '-max_target_seqs', '1', '-num_threads', '1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elif debug:
        subprocess.run(
            ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db',
             dataSubmissionInQ.reference_fasta_database_used, '-max_target_seqs', '1', '-num_threads', '1'])

    # Read in blast output
    blast_output_file = readDefinedFileToList(r'{}/blast.out'.format(wkd))
    if debug:
        if not blast_output_file:
            print('WARNING blast output file is empty for evalue screening')
        else:
            if len(blast_output_file) < 10:
                print('WARNING blast output file for evalue screening has only {} lines'.format(len(blast_output_file)))

    # now create a below_e_cutoff_seq to clade dictionary
    sub_e_seq_to_clade_dict = {a.split('\t')[0]: a.split('\t')[1][-1] for a in blast_output_file}
    # print out the fasta with the clades appended to the end
    fasta_out_with_clade = []
    for line in fasta_out:
        if line[0] == '>':
            fasta_out_with_clade.append(line + '_clade' + sub_e_seq_to_clade_dict[line[1:]])
        else:
            fasta_out_with_clade.append(line)
    return fasta_out_with_clade


def make_evalue_screening_fasta_no_clade(dSID, e_value_multiP_dict, wkd):
    below_e_cutoff_dict = dict(e_value_multiP_dict)
    temp_count = 0
    fasta_out = []
    for key, value in below_e_cutoff_dict.items():
        if value > 2:
            # then this is a sequences that was found in three or more samples
            fasta_out.extend(['>sub_e_seq_count_{}_{}_{}'.format(temp_count, dSID, value), key])
            temp_count += 1
    if fasta_out:
        writeListToDestination(wkd + '/blastInputFasta.fa', fasta_out)
    return fasta_out


def perform_sequence_drop():
    sequence_drop_file = generate_sequence_drop_file()
    sequence_drop_path = '{}{}{}'.format(
        os.path.dirname(__file__),
        '/dbBackUp/seq_dumps/seq_dump_',
        str(datetime.now()).replace(' ', '_', ).replace(':', '-'))
    sys.stdout.write('\n\nBackup of named reference_sequences output to {}\n'.format(sequence_drop_path))
    writeListToDestination(sequence_drop_path, sequence_drop_file)


def processed_samples_status(dataSubmissionInQ, pathToInputFile):
    sampleList = data_set_sample.objects.filter(dataSubmissionFrom=dataSubmissionInQ)
    failedList = []
    for sample in sampleList:
        if sample.errorInProcessing:
            failedList.append(sample.name)
    readMeList = []
    sumMessage = '\n\n{0} out of {1} samples successfully passed QC.\n' \
                 '{2} samples produced erorrs\n'.format((len(sampleList) - len(failedList)), len(sampleList),
                                                        len(failedList))
    print(sumMessage)
    readMeList.append(sumMessage)
    for sample in sampleList:

        if sample.name not in failedList:
            print('Sample {} processed successfuly'.format(sample.name))
            readMeList.append('Sample {} processed successfuly'.format(sample.name))
        else:
            print('Sample {} : {}'.format(sample.name, sample.errorReason))
    for sampleName in failedList:
        readMeList.append('Sample {} : ERROR in sequencing reads. Unable to process'.format(sampleName))
    writeListToDestination(pathToInputFile + '/readMe.txt', readMeList)


def taxonomic_screening(wkd, dSID, numProc, dataSubmissionInQ, error_sample_list, screen_sub_e, output_dir, debug):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))

    # If we will be screening the seuqences
    # At this point we should create a back up of the current symClade db.
    # if not screening. no need to back up.
    if screen_sub_e:
        # we should be able to make this much fasta by having a list that keeps track of which samples have
        # already reported having 0 sequences thrown out due to being too divergent from reference sequences
        # we should populate this list when we are checking a sample in execute_worker_taxa_screening
        checked_samples = []
        new_seqs_add_to_symClade_db_count = 0
        create_symClade_backup(dSID)

        # here we continue to do screening until we add no more sub_evalue seqs to the symClade.fa ref db
        # or until we find no sub_e_seqs in the first place
        # a sub e seq is one that did return a match when run against the symClade database (blast) but was below
        # the e value cuttoff required. We screen these to make sure that we are not thowing away symbiodinum sequences
        # just because they don't have representatives in the references symCladedb.
        found = True
        while found:
            # everytime that the execute_worker is run it pickles out the files needed for the next worker
            e_value_multiP_dict, checked_samples = execute_worker_taxa_screening(dataSubmissionInQ, error_sample_list,
                                                                                 numProc,
                                                                                 sampleFastQPairs, wkd, debug=debug,
                                                                                 checked_samples=checked_samples)
            if len(e_value_multiP_dict) == 0:
                # then there are no sub_e_seqs to screen and we can exit
                break

            # this is where we will do the screening.
            # the outcome of this should be an updated symClade.fa that we should then make a blastdb from
            # if we indeed find that some of the sequences that were below the evalue cut off are symbiodinium
            # then this should return True. else False.

            # From the e_value_multiP_dict generate a fasta of the sequences that were found in more than 3 samples
            # pass this to the screen_sub_e_seqs

            # The number of samples a sequence must have been found in for us to consider it for screening is taken into
            # account when the fasta is written out.
            # This function will reuturn False if we end up with no sequences to screen
            # It will outherwise reutrn the list that is the fasta that was written out.
            fasta_out, fasta_out_path = generate_and_write_below_evalue_fasta_for_screening(dSID, dataSubmissionInQ,
                                                                                            e_value_multiP_dict, wkd,
                                                                                            debug)
            # we only need to screen if we have a fasta to screen
            if fasta_out:
                # here found represents whether we found any of the seqs to be symbiodinium
                # if found is returned then no change has been made to the symcladedb.
                found, new_seqs = screen_sub_e_seqs(data_set_id=dSID, wkd=wkd)
                new_seqs_add_to_symClade_db_count += new_seqs
            else:
                found = False

    else:
        # if not doing the screening we can simply run the execute_worker_ta... once.
        # during its run it will have output all of the files we need to run the following workers.
        # we can also run the generate_and_write_below... function to write out a fast of significant sequences
        # we can then report to the user using that object
        e_value_multiP_dict = execute_worker_taxa_screening(dataSubmissionInQ, error_sample_list, numProc,
                                                            sampleFastQPairs, wkd, debug=debug)

        fasta_out, fasta_out_path = generate_and_write_below_evalue_fasta_for_screening(dSID, dataSubmissionInQ,
                                                                                        e_value_multiP_dict, wkd, debug)

    # input_q, wkd, data_sub_id
    # worker_taxonomy_write_out
    # Create the queues that will hold the sample information
    input_q = Queue()

    # The list that contains the names of the samples that returned errors during the initial mothur
    worker_manager = Manager()
    error_sample_list_shared = worker_manager.list(error_sample_list)

    # we want to collect all of the discarded sequences so that we can print this to a fasta and
    # output this in the data_set's submission output directory
    # to do this we'll need a list that we can use to collect all of these sequences
    # once we have collected them all we can then set them and use this to make a fasta to output
    # we want to do this on a sample by sample basis, so we now write out directly from
    # the worker as well as doing the 'total' method.
    # we have already created the output dir early on and I will pass it down to here so that we can pass
    # it to the below method
    list_of_discarded_sequences = worker_manager.list()

    # create the directory that will be used for the output for the output of the throw away sequences on
    # a sample by sample basis (one fasta and one name file per sample)
    throw_away_seqs_dir = '{}/throw_awayseqs'.format(output_dir)
    os.makedirs(throw_away_seqs_dir, exist_ok=True)

    # load up the input q
    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)

    # load in the STOPs
    for n in range(numProc):
        input_q.put('STOP')

    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')

    for n in range(numProc):
        p = Process(target=worker_taxonomy_write_out, args=(
            input_q, error_sample_list_shared, wkd, dSID, list_of_discarded_sequences, throw_away_seqs_dir))
        all_processes.append(p)
        p.start()

    for p in all_processes:
        p.join()

    # now create a fasta from the list
    discarded_seqs_fasta = []
    discarded_seqs_name_counter = 0
    for seq in set(list(list_of_discarded_sequences)):
        discarded_seqs_fasta.extend(['>discard_seq_{}_data_sub_{}'.format(discarded_seqs_name_counter, dSID), seq])
        discarded_seqs_name_counter += 1

    if screen_sub_e:
        # If we are screening then we want to be returning the number of seqs added
        return new_seqs_add_to_symClade_db_count, discarded_seqs_fasta
    else:
        # if we are not screening then we want to return the fasta that contains the significant sub_e_value seqs
        return fasta_out, fasta_out_path, discarded_seqs_fasta


def create_symClade_backup(dSID):
    back_up_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB', 'symClade_backup'))
    os.makedirs(back_up_dir, exist_ok=True)
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')) + '/symClade.fa'
    time_stamp = str(datetime.now()).replace(' ', '_').replace(':', '-')
    dst_fasta_path = back_up_dir + '/symClade_{}.fa'.format(time_stamp)
    dst_readme_path = back_up_dir + '/symClade_{}.readme'.format(time_stamp)
    # then write a copy to it.
    shutil.copyfile(src_path, dst_fasta_path)
    # Then write out a very breif readme
    read_me = ['This is a symClade.fa backup created during datasubmission of data_set ID: {}'.format(dSID)]
    writeListToDestination(dst_readme_path, read_me)


def screen_sub_e_seqs(wkd, data_set_id, required_symbiodinium_matches=3,
                      full_path_to_nt_database_directory='/home/humebc/phylogeneticSoftware/ncbi-blast-2.6.0+/ntdbdownload',
                      num_proc=20):
    '''This function screens a fasta file to see if the sequences are Symbiodinium in origin.
    This fasta file contains the below_e_cutoff values that need to be screened. It only contains seuqences that
    were found in at least 3 samples.
    We will call a sequence symbiodinium if it has a match that covers at least 95% of its sequence at a 60% or higher
    identity. It must also have Symbiodinium or Symbiodiniaceae in the name. We will also require that a
    sub_e_value seq has at least the required_sybiodinium_matches (3 at the moment) before we call it SYmbiodinium.'''

    # Write out the hidden file that points to the ncbi database directory.
    ncbircFile = []

    db_path = full_path_to_nt_database_directory
    ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])

    writeListToDestination("{}/.ncbirc".format(wkd), ncbircFile)

    # Read in the fasta files of below e values that were kicked out. This has already been filtered to only
    # contain seqs that were found in > 3 samples.
    path_to_input_fasta = '{}/below_e_cutoff_seqs_{}.fasta'.format(wkd, data_set_id)
    fasta_file = readDefinedFileToList(path_to_input_fasta)
    fasta_file_dict = createDictFromFasta(fasta_file)

    # screen the input fasta for sample support according to seq_sample_support_cut_off
    # screened_fasta = []
    # for i in range(len(fasta_file)):
    #     if fasta_file[i][0] == '>':
    #         if int(fasta_file[i].split('_')[5]) >= seq_sample_support_cut_off:
    #             screened_fasta.extend([fasta_file[i], fasta_file[i + 1]])

    # write out the screened fasta so that it can be read in to the blast
    # make sure to reference the sequence support and the iteration
    # path_to_screened_fasta = '{}/{}_{}_{}.fasta'.format(data_sub_data_dir,
    #                                                     'below_e_cutoff_seqs_{}.screened'.format(ds_id), iteration_id,
    #                                                     seq_sample_support_cut_off)
    # screened_fasta_dict = createDictFromFasta(screened_fasta)
    # writeListToDestination(path_to_screened_fasta, screened_fasta)

    # Set up environment for running local blast
    blastOutputPath = '{}/blast_eval.out'.format(wkd)
    outputFmt = "6 qseqid sseqid staxids evalue pident qcovs staxid stitle ssciname"
    # inputPath = r'{}/below_e_cutoff_seqs.fasta'.format(data_sub_data_dir)
    os.chdir(wkd)

    # Run local blast
    completed_process = subprocess.run(
        ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', path_to_input_fasta, '-db', 'nt',
         '-max_target_seqs', '10', '-num_threads', str(num_proc)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Read in blast output
    blast_output_file = readDefinedFileToList(blastOutputPath)

    # Now go through each of the results and look to see if there is a result that has > 95% coverage and has >60%
    # match and has symbiodinium in the name.
    # if you find a number that equals the required_symbiodinium_matches then
    # add the name of this seq to the reference db

    # create a dict that is the query name key and a list of subject return value
    blast_output_dict = defaultdict(list)
    for line in blast_output_file:
        blast_output_dict[line.split('\t')[0]].append('\t'.join(line.split('\t')[1:]))

    verified_sequence_list = []
    for k, v in blast_output_dict.items():
        sym_count = 0
        for result_str in v:
            if 'Symbiodinium' in result_str:
                percentage_coverage = float(result_str.split('\t')[4])
                percentage_identity_match = float(result_str.split('\t')[3])
                if percentage_coverage > 95 and percentage_identity_match > 60:
                    sym_count += 1
                    if sym_count == required_symbiodinium_matches:
                        verified_sequence_list.append(k)
                        break

    # We only need to proceed from here to make a new database if we have sequences that have been verified as
    # Symbiodinium
    if verified_sequence_list:
        sym_db_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
        # here we have a list of the Symbiodinium sequences that we can add to the reference db fasta
        new_fasta = []
        for seq_to_add in verified_sequence_list:
            new_fasta.extend(['>{}'.format(seq_to_add), '{}'.format(fasta_file_dict[seq_to_add])])

        # now add the current sequences
        previous_reference_fasta = readDefinedFileToList(
            '{}/{}'.format(sym_db_dir, 'symClade.fa'))

        combined_fasta = new_fasta + previous_reference_fasta

        # now that the reference db fasta has had the new sequences added to it.
        # write out to the db to the database directory of SymPortal
        full_path_to_new_db = '{}/symClade.fa'.format(sym_db_dir)
        writeListToDestination(full_path_to_new_db, combined_fasta)

        # run makeblastdb
        completed_process = subprocess.run(
            ['makeblastdb', '-in', full_path_to_new_db, '-dbtype', 'nucl', '-title', 'symClade'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return True, int(len(new_fasta) / 2)
    else:
        return False, 0


def execute_worker_taxa_screening(dataSubmissionInQ, error_sample_list, numProc, sampleFastQPairs, wkd, debug,
                                  checked_samples=None):
    # The error sample list is a list that houses the sample names that failed in the inital mothur QC worker

    # Create the queues that will hold the sample information
    input_q = Queue()

    # This will be a dictionary that we use to keep track of sequences that are found as matches when we do the
    # blast search against the symClade.fa database but that fall below the e value cutoff which is currently set
    # at e^-100. It will be a dictionary of sequence to number of samples in which the sequence was found in
    # the logic being that if we find sequences in multiple samples then they are probably genuine sequences
    # and they should therefore be checked against the full blast database to see if they match Symbiodinium
    # if they do then they should be put into the database.
    worker_manager = Manager()
    e_value_multiP_dict = worker_manager.dict()

    # Create an MP list of the error_sample_list so that we can use it thread safe.
    error_sample_list_shared = worker_manager.list(error_sample_list)

    if checked_samples is not None:
        # list to check which of the samples have already had 0 seqs thown out initially
        checked_samples_shared = worker_manager.list(checked_samples)

    # load up the input q
    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)

    # load in the STOPs
    for n in range(numProc):
        input_q.put('STOP')
    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')
    for n in range(numProc):
        if checked_samples is not None:
            p = Process(target=worker_taxonomy_screening,
                        args=(input_q, wkd, dataSubmissionInQ.reference_fasta_database_used,
                              e_value_multiP_dict, error_sample_list_shared, debug, checked_samples_shared))
        else:
            p = Process(target=worker_taxonomy_screening,
                        args=(input_q, wkd, dataSubmissionInQ.reference_fasta_database_used,
                              e_value_multiP_dict, error_sample_list_shared, debug))
        all_processes.append(p)
        p.start()
    for p in all_processes:
        p.join()
    if checked_samples is not None:
        return e_value_multiP_dict, list(checked_samples_shared)
    else:
        return e_value_multiP_dict


def worker_taxonomy_screening(input_q, wkd, reference_db_name, e_val_collection_dict, err_smpl_list, debug,
                              checked_samples=None):
    for contigPair in iter(input_q.get, 'STOP'):
        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        # If the sample gave an error during the inital mothur then we don't consider it here.
        if sampleName in err_smpl_list:
            continue
        # A sample will be in this list if we have already performed this worker on it and it came
        # up as having 0 sequences thrown out initially due to being too divergent from
        if checked_samples is not None:
            if sampleName in checked_samples:
                continue

        current_directory = r'{0}/{1}/'.format(wkd, sampleName)
        root_name = r'{0}stability'.format(sampleName)

        ncbircFile = []
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
        ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])

        # Run local blast of all seqs and determine clade. Discard seqs below evalue cutoff
        # and write out new fasta, name, group and clade dict
        sys.stdout.write('{}: verifying seqs are Symbiodinium and determining clade\n'.format(sampleName))

        # write the .ncbirc file that gives the location of the db
        writeListToDestination("{0}.ncbirc".format(current_directory), ncbircFile)

        # Read in the fasta, name and group files and convert to dics
        fastaFile = readDefinedFileToList(
            '{0}{1}.trim.contigs.good.unique.abund.pcr.unique.fasta'.format(current_directory, root_name))
        uniqueFastaFile = createNoSpaceFastaFile(fastaFile)
        writeListToDestination('{}blastInputFasta.fa'.format(current_directory), uniqueFastaFile)
        fastaDict = createDictFromFasta(uniqueFastaFile)
        nameFile = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.names'.format(current_directory, root_name))
        nameDict = {a.split('\t')[0]: a for a in nameFile}

        groupFile = readDefinedFileToList('{0}{1}.contigs.good.abund.pcr.groups'.format(current_directory, root_name))

        # Set up environment for running local blast

        blastOutputPath = r'{}blast.out'.format(current_directory)
        outputFmt = "6 qseqid sseqid staxids evalue pident qcovs"
        inputPath = r'{}blastInputFasta.fa'.format(current_directory)
        os.chdir(current_directory)

        # Run local blast
        if not debug:
            completed_process = subprocess.run(
                ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', reference_db_name,
                 '-max_target_seqs', '1', '-num_threads', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        elif debug:
            subprocess.run(
                ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', reference_db_name,
                 '-max_target_seqs', '1', '-num_threads', '1'])
        sys.stdout.write('{}: BLAST complete\n'.format(sampleName))
        # Read in blast output
        blastOutputFile = readDefinedFileToList(r'{}blast.out'.format(current_directory))
        if debug:
            if not blastOutputFile:
                print('WARNING blast output file is empty for {}'.format(sampleName))
            else:
                if len(blastOutputFile) < 10:
                    print('WARNING blast output file for {} is only {} lines long'.format(sampleName,
                                                                                          len(blastOutputFile)))

        # this is a clade dict. I'm not sure why I called it blast Dict. It is sequences name to clade.
        blastDict = {a.split('\t')[0]: a.split('\t')[1][-1] for a in blastOutputFile}
        throwAwaySeqs = []

        # Uncomment for blasting QC
        # NB it turns out that the blast will not always return a match.
        # If a match is not returned it means the sequence did not have a significant match to the seqs in the db
        # Add any seqs that did not return a blast match to the throwAwaySeq list
        diff = set(fastaDict.keys()) - set(blastDict.keys())
        throwAwaySeqs.extend(list(diff))
        sys.stdout.write(
            '{}: {} sequences thrown out initially due to being too divergent from reference sequences\n'.format(
                sampleName, len(list(diff))))

        # NB note that the blast results sometime return several matches for the same seq.
        # as such we will use the already_processed_blast_seq_resulst to make sure that we only
        # process each sequence once.
        already_processed_blast_seq_result = []
        for line in blastOutputFile:
            seq_in_q = line.split('\t')[0]
            identity = float(line.split('\t')[4])
            coverage = float(line.split('\t')[5])
            if seq_in_q in already_processed_blast_seq_result:
                continue
            already_processed_blast_seq_result.append(seq_in_q)
            try:
                evaluePower = int(line.split('\t')[3].split('-')[1])
                # TODO with the smallest sequences i.e. 185bp it is impossible to get above the 100 threshold
                # even if there is an exact match. As such, we sould also look at the match identity and coverage
                if evaluePower < 100:  # evalue cut off, collect sequences that don't make the cut
                    if identity < 80 or coverage < 95:
                        throwAwaySeqs.append(seq_in_q)
                        # incorporate the size cutoff here that would normally happen below
                        if 184 < len(fastaDict[seq_in_q]) < 310:
                            if fastaDict[seq_in_q] in e_val_collection_dict.keys():
                                e_val_collection_dict[fastaDict[seq_in_q]] += 1
                            else:
                                e_val_collection_dict[fastaDict[seq_in_q]] = 1
            except:
                # here we weren't able to extract the evaluePower for some reason.
                if identity < 80 or coverage < 95:
                    throwAwaySeqs.append(seq_in_q)
                    if 184 < len(fastaDict[seq_in_q]) < 310:
                        if fastaDict[seq_in_q] in e_val_collection_dict.keys():
                            e_val_collection_dict[fastaDict[seq_in_q]] += 1
                        else:
                            e_val_collection_dict[fastaDict[seq_in_q]] = 1

        if checked_samples is not None and not throwAwaySeqs:
            # if we see that there were no seqs thrown away from this sample then we don't need to be checking it in
            # the further iterations so we can add it to the checked_samples list
            checked_samples.append(sampleName)

        # here we will pickle out all of the items that we are going to need for the next worker.
        pickle.dump(nameDict, open("{}/name_dict.pickle".format(current_directory), "wb"))
        pickle.dump(fastaDict, open("{}/fasta_dict.pickle".format(current_directory), "wb"))
        pickle.dump(throwAwaySeqs, open("{}/throw_away_seqs.pickle".format(current_directory), "wb"))
        pickle.dump(groupFile, open("{}/group_file.pickle".format(current_directory), "wb"))
        pickle.dump(blastDict, open("{}/blast_dict.pickle".format(current_directory), "wb"))


def worker_taxonomy_write_out(input_q, error_sample_list_shared, wkd, data_sub_id, list_of_discarded_seqs,
                              throw_away_seqs_dir):
    dataSubInQ = data_set.objects.get(id=data_sub_id)
    for contigPair in iter(input_q.get, 'STOP'):
        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        if sampleName in error_sample_list_shared:
            continue
        dataSetSampleInstanceInQ = data_set_sample.objects.get(name=sampleName, dataSubmissionFrom=dataSubInQ)
        current_directory = r'{0}/{1}/'.format(wkd, sampleName)
        root_name = r'{0}stability'.format(sampleName)

        # Get the pickled items that we're going to need in this worker
        nameDict = pickle.load(open("{}/name_dict.pickle".format(current_directory), "rb"))
        fastaDict = pickle.load(open("{}/fasta_dict.pickle".format(current_directory), "rb"))
        throwAwaySeqs = pickle.load(open("{}/throw_away_seqs.pickle".format(current_directory), "rb"))
        groupFile = pickle.load(open("{}/group_file.pickle".format(current_directory), "rb"))
        blastDict = pickle.load(open("{}/blast_dict.pickle".format(current_directory), "rb"))

        # NB it turns out that sometimes a sequence is returned in the blast results twice! This was messing up
        # our meta-analysis reporting. This will be fixed by working with sets of the throwaway sequences
        # Also create a fasta that can be output which will contain the binned sequences so that they can
        # be checked if needs be

        # also here generate a fasta a name file of the throwAwaySeqs that will be written out on a sample by sample
        # basis in the output directory. This will allow us to analyses the samples that have been thrown away
        temp_count = 0
        throw_away_fasta = []
        throw_away_name = []
        for seq_name in list(set(throwAwaySeqs)):
            temp_count += len(nameDict[seq_name].split('\t')[1].split(','))
            list_of_discarded_seqs.append(fastaDict[seq_name])
            throw_away_fasta.append('>{}'.format(seq_name))
            throw_away_fasta.append('{}'.format(fastaDict[seq_name]))
            throw_away_name.append(nameDict[seq_name])

        # now write out the throw_away fasta and name files
        # make sure that the sample specific throwaway seq dir exists
        if throw_away_fasta:
            sample_throw_away_seqs_dir = '{}/{}'.format(throw_away_seqs_dir, sampleName)
            os.makedirs(sample_throw_away_seqs_dir, exist_ok=True)

            with open('{}/{}_throw_away_seqs.fasta'.format(sample_throw_away_seqs_dir, sampleName), 'w') as f:
                for line in throw_away_fasta:
                    f.write('{}\n'.format(line))

            with open('{}/{}_throw_away_seqs.name'.format(sample_throw_away_seqs_dir, sampleName), 'w') as f:
                for line in throw_away_name:
                    f.write('{}\n'.format(line))

        dataSetSampleInstanceInQ.non_sym_absolute_num_seqs = temp_count
        # Add details of non-symbiodinium unique seqs
        dataSetSampleInstanceInQ.nonSymSeqsNum = len(set(throwAwaySeqs))
        dataSetSampleInstanceInQ.save()

        # Output new fasta, name and group files that don't contain seqs that didn't make the cut

        sys.stdout.write(
            '{}: discarding {} unique sequences for evalue cutoff violations\n'.format(sampleName,
                                                                                       str(len(throwAwaySeqs))))
        newFasta = []
        newName = []
        newGroup = []
        cladalDict = {}
        count = 0
        listOfBadSeqs = []
        for line in groupFile:
            sequence = line.split('\t')[0]
            if sequence not in throwAwaySeqs:
                newGroup.append(line)
                # The fastaDict is only meant to have the unique seqs in so this will
                # go to 'except' a lot. This is OK and normal
                try:
                    newFasta.extend(['>{}'.format(sequence), fastaDict[sequence]])
                except:
                    pass

                try:
                    newName.append(nameDict[sequence])
                    listOfSameSeqNames = nameDict[sequence].split('\t')[1].split(',')
                    clade = blastDict[sequence]

                    for seqName in listOfSameSeqNames:
                        cladalDict[seqName] = clade
                except:
                    pass
        # Now write the files out
        if not newFasta:
            # Then the fasta is blank and we have got no good Symbiodinium seqs
            errorReason = 'No Symbiodinium sequences left after blast annotation'
            log_qc_error_and_continue(dataSetSampleInstanceInQ, sampleName, errorReason)
            error_sample_list_shared.put(sampleName)
            continue
        sys.stdout.write('{}: non-Symbiodinium sequences binned\n'.format(sampleName))
        writeListToDestination('{0}{1}.trim.contigs.good.unique.abund.pcr.blast.fasta'.format(current_directory, root_name),
                               newFasta)
        writeListToDestination('{0}{1}.trim.contigs.good.abund.pcr.blast.names'.format(current_directory, root_name), newName)
        writeListToDestination('{0}{1}.contigs.good.abund.pcr.blast.groups'.format(current_directory, root_name), newGroup)
        writeByteObjectToDefinedDirectory('{0}{1}.cladeDict.dict'.format(current_directory, root_name), cladalDict)
    return


def worker_screen_size(input_q, error_sample_list, wkd, data_sub_id, debug):
    dataSubInQ = data_set.objects.get(id=data_sub_id)
    for contigPair in iter(input_q.get, 'STOP'):
        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        if sampleName in error_sample_list:
            continue

        dataSetSampleInstanceInQ = data_set_sample.objects.get(name=sampleName, dataSubmissionFrom=dataSubInQ)
        current_directory = r'{0}/{1}/'.format(wkd, sampleName)
        root_name = r'{0}stability'.format(sampleName)
        # At this point we have the newFasta, newName, newGroup. These all only contain sequences that were
        # above the blast evalue cutoff.
        # We also have the cladalDict for all of these

        # Now finish off the mothur analyses by discarding by size range

        # I am now going to switch this to an absolute size range as I am having problems with Mani's sequences.
        # For some reason he is having an extraordinarily high number of very short sequence (i.e. 15bp long).
        # These are not being thrown out in the blast work. As such the average is being thrown off. and means that our
        # upper size limit is only about 200.
        # I have calculated the averages of each of the clades for our reference sequences so far
        '''Clade A 234.09815950920245
            Clade B 266.79896907216494
            Clade C 261.86832986832985
            Clade D 260.44158075601376
             '''
        # I will take our absolute cutoffs from these numbers (+- 50 bp) so 184-310
        last_summary = readDefinedFileToList(
            '{0}{1}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(current_directory, root_name))
        sum = 0
        for line in last_summary[1:]:
            sum += int(line.split('\t')[3])
        average = int(sum / len(last_summary))
        cutOffLower = 184
        cutOffUpper = 310

        secondmothur_batch_file = [
            r'set.dir(input={0})'.format(current_directory),
            r'set.dir(output={0})'.format(current_directory),
            r'screen.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.fasta, '
            r'name={0}{1}.trim.contigs.good.abund.pcr.blast.names, '
            r'group={0}{1}.contigs.good.abund.pcr.blast.groups,  minlength={2}, maxlength={3})'.format(
                current_directory, root_name, cutOffLower, cutOffUpper),
            r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.fasta, '
            r'name={0}{1}.trim.contigs.good.abund.pcr.blast.good.names)'.format(
                current_directory, root_name),
            r'unique.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.fasta, '
            r'name={0}{1}.trim.contigs.good.abund.pcr.blast.good.names)'.format(
                current_directory, root_name),
            r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.unique.fasta, '
            r'name={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.names)'.format(
                current_directory, root_name),
        ]
        mothur_batch_file_path = r'{0}{1}{2}'.format(current_directory, 'mothur_batch_file', root_name)

        writeListToDestination(mothur_batch_file_path, secondmothur_batch_file)

        completed_process = subprocess.run(['mothur', r'{0}'.format(mothur_batch_file_path)], stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)

        if completed_process.returncode == 1 or 'ERROR' in completed_process.stdout.decode('utf-8'):
            if debug:
                print(completed_process.stdout.decode('utf-8'))
            errorReason = 'No Symbiodinium sequences left after size screening'
            log_qc_error_and_continue(dataSetSampleInstanceInQ, sampleName, errorReason)
            error_sample_list.put(sampleName)
            continue
    return


def worker_write_out_clade_separated_fastas(input_q, error_sample_list, wkd, data_sub_id):
    dataSubInQ = data_set.objects.get(id=data_sub_id)
    for contigPair in iter(input_q.get, 'STOP'):
        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        if sampleName in error_sample_list:
            continue
        dataSetSampleInstanceInQ = data_set_sample.objects.get(name=sampleName, dataSubmissionFrom=dataSubInQ)
        current_directory = r'{0}/{1}/'.format(wkd, sampleName)
        root_name = r'{0}stability'.format(sampleName)

        # Here make cladally separated fastas

        try:
            fastaFile = readDefinedFileToList(
                '{0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.unique.fasta'.format(current_directory, root_name))
            nameFile = readDefinedFileToList(
                '{0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.names'.format(current_directory, root_name))
        except FileNotFoundError:
            log_qc_error_and_continue(dataSetSampleInstanceInQ, sampleName, 'generic_error')
            continue

        sys.stdout.write('{}: final Mothur completed\n'.format(sampleName))
        fastaDict = createDictFromFasta(fastaFile)

        nameDict = {a.split('\t')[0]: a for a in nameFile}
        cladeDict = readByteObjectFromDefinedDirectory('{0}{1}.cladeDict.dict'.format(current_directory, root_name))
        cladeDirs = []
        cladeFastas = {}
        for line in nameFile:
            sequence = line.split('\t')[0]
            clade = cladeDict[sequence]
            if clade in cladeDirs:  # Already have a dir for it
                cladeFastas[clade][0].extend(['>{}'.format(sequence), fastaDict[sequence]])
                cladeFastas[clade][1].append(nameDict[sequence])

            else:  # Make dir and add empty fasta list to cladeFastas
                cladeFastas[clade] = ([], [])
                cladeDirs.append(clade)
                os.makedirs(r'{}{}'.format(current_directory, clade), exist_ok=True)
                cladeFastas[clade][0].extend(['>{}'.format(sequence), fastaDict[sequence]])
                cladeFastas[clade][1].append(nameDict[sequence])

        total_debug_absolute = 0
        total_debug_unique = 0

        for someclade in cladeDirs:
            # These are the files that we are going to want to put into the pre_MED_seqs directory
            # By doing this, people will be able to work without the MED component of the SP QC
            writeListToDestination(
                r'{0}{1}/{2}.QCed.clade{1}.fasta'.format(current_directory, someclade, root_name.replace('stability', '')),
                cladeFastas[someclade][0])
            writeListToDestination(
                r'{0}{1}/{2}.QCed.clade{1}.names'.format(current_directory, someclade, root_name.replace('stability', '')),
                cladeFastas[someclade][1])

            # write the files into the
            wkd.replace('tempData', 'pre_MED_seqs')

            writeListToDestination(
                r'{0}{1}/{2}.QCed.clade{1}.fasta'.format(current_directory.replace('tempData', 'pre_MED_seqs'), someclade,
                                                         root_name.replace('stability', '')),
                cladeFastas[someclade][0])
            writeListToDestination(
                r'{0}{1}/{2}.QCed.clade{1}.names'.format(current_directory.replace('tempData', 'pre_MED_seqs'), someclade,
                                                         root_name.replace('stability', '')),
                cladeFastas[someclade][1])

        pickle.dump(nameDict, open("{}/name_dict.pickle".format(current_directory), "wb"))
    return


def execute_size_screening(wkd, numProc, dSID, error_sample_list, debug):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    # Create the queues that will hold the sample information
    input_q = Queue()

    # the list that contains the sampleNames that have failed so far
    worker_manager = Manager()
    error_sample_list_shared = worker_manager.list(error_sample_list)

    # load up the input q
    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)

    # load in the STOPs
    for n in range(numProc):
        input_q.put('STOP')

    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')

    for n in range(numProc):
        p = Process(target=worker_screen_size, args=(input_q, error_sample_list_shared, wkd, dSID, debug))
        all_processes.append(p)
        p.start()

    for p in all_processes:
        p.join()
    return


def write_out_clade_separated_fastas(wkd, numProc, dSID, error_sample_list):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    # Create the queues that will hold the sample information
    input_q = Queue()

    # the list that contains the sampleNames that have failed so far
    worker_manager = Manager()
    error_sample_list_shared = worker_manager.list(error_sample_list)

    # load up the input q
    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)

    # load in the STOPs
    for n in range(numProc):
        input_q.put('STOP')

    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')

    for n in range(numProc):
        p = Process(target=worker_write_out_clade_separated_fastas, args=(input_q, error_sample_list_shared, wkd, dSID))
        all_processes.append(p)
        p.start()

    for p in all_processes:
        p.join()
    return


def associate_QC_meta_data_to_samples(wkd, numProc, dSID, error_sample_list):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    # Create the queues that will hold the sample information
    input_q = Queue()

    # the list that contains the sampleNames that have failed so far
    worker_manager = Manager()
    error_sample_list_shared = worker_manager.list(error_sample_list)

    # load up the input q
    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)

    # load in the STOPs
    for n in range(numProc):
        input_q.put('STOP')

    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')

    for n in range(numProc):
        p = Process(target=worker_associate_QC_meta_data_to_samples,
                    args=(input_q, error_sample_list_shared, wkd, dSID))
        all_processes.append(p)
        p.start()

    for p in all_processes:
        p.join()
    return


def worker_associate_QC_meta_data_to_samples(input_q, error_sample_list, wkd, data_sub_id):
    dataSubInQ = data_set.objects.get(id=data_sub_id)
    for contigPair in iter(input_q.get, 'STOP'):

        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        if sampleName in error_sample_list:
            continue

        dataSetSampleInstanceInQ = data_set_sample.objects.get(name=sampleName, dataSubmissionFrom=dataSubInQ)
        current_directory = r'{0}/{1}/'.format(wkd, sampleName)
        root_name = r'{0}stability'.format(sampleName)

        # load the nameDict form the previous worker
        nameDict = pickle.load(open("{}/name_dict.pickle".format(current_directory), "rb"))
        # Here we have cladaly sorted fasta and name file in new directory

        # now populate the data set sample with the qc meta-data
        # get unique seqs remaining
        dataSetSampleInstanceInQ.finalUniqueSeqNum = len(nameDict)
        # Get total number of sequences
        count = 0
        for nameKey in nameDict.keys():
            count += len(nameDict[nameKey].split('\t')[1].split(','))
        dataSetSampleInstanceInQ.finalTotSeqNum = count
        # now get the seqs lost through size violations through subtraction
        dataSetSampleInstanceInQ.size_violation_absolute = \
            dataSetSampleInstanceInQ.post_seq_qc_absolute_num_seqs - \
            dataSetSampleInstanceInQ.finalTotSeqNum - \
            dataSetSampleInstanceInQ.non_sym_absolute_num_seqs
        dataSetSampleInstanceInQ.size_violation_unique = \
            dataSetSampleInstanceInQ.initialUniqueSeqNum - \
            dataSetSampleInstanceInQ.finalUniqueSeqNum - \
            dataSetSampleInstanceInQ.nonSymSeqsNum

        # Now update the data_set_sample instance to set initialProcessingComplete to True
        dataSetSampleInstanceInQ.initialProcessingComplete = True
        dataSetSampleInstanceInQ.save()
        sys.stdout.write('{}: initial processing complete\n'.format(sampleName))
        sys.stdout.write('{}: dataSetSampleInstanceInQ.finalUniqueSeqNum = {}\n'.format(sampleName, len(nameDict)))
        sys.stdout.write('{}: dataSetSampleInstanceInQ.finalTotSeqNum = {}\n'.format(sampleName, count))

        os.chdir(current_directory)
        fileList = [f for f in os.listdir(current_directory) if f.endswith((".names", ".fasta", ".qual", ".summary", ".oligos",
                                                                     ".accnos", ".files", ".groups", ".logfile",
                                                                     ".dict",
                                                                     ".fa",
                                                                     ".out"))]
        for f in fileList:
            os.remove(f)

        sys.stdout.write('{}: pre-MED processing completed\n'.format(sampleName))
    return


def preMED_QC(dSID, dataSubmissionInQ, numProc, wkd, screen_sub_evalue, output_dir, debug):
    # check to see whether the reference_fasta_database_used has been created
    # we no longer by default have the blast binaries already made so that we don't have to have them up on
    # github. As such if this is the first time or if there has been an update of something
    # we should create the bast dictionary from the .fa
    validate_taxon_screening_ref_blastdb(dataSubmissionInQ, debug)
    # this method will perform the bulk of the QC (everything before MED). The output e_value_mltiP_dict
    # will be used for screening the sequences that were found in multiple samples but were not close enough
    # to a sequence in the refreence database to be included outright in the analysis.

    # First execute the initial mothur QC. This should leave us with a set of fastas, name and groupfiles etc in
    # a directory from each sample.
    error_sample_list = execute_worker_initial_mothur(dSID, numProc, wkd, debug)

    # Now do the iterative screening
    # this should contain two parts, the screening and the handling of the screening results
    # it should also contain the writing out of the screened seqs.
    if screen_sub_evalue:
        new_seqs_added_count, discarded_seqs_fasta = taxonomic_screening(dSID=dSID, dataSubmissionInQ=dataSubmissionInQ,
                                                                         wkd=wkd,
                                                                         numProc=numProc,
                                                                         error_sample_list=error_sample_list,
                                                                         screen_sub_e=screen_sub_evalue,
                                                                         output_dir=output_dir, debug=debug)
    else:
        fasta_of_sig_sub_e_seqs, fasta_of_sig_sub_e_seqs_path, discarded_seqs_fasta = \
            taxonomic_screening(dSID=dSID, dataSubmissionInQ=dataSubmissionInQ, wkd=wkd, numProc=numProc,
                                error_sample_list=error_sample_list, screen_sub_e=screen_sub_evalue,
                                output_dir=output_dir, debug=debug)

    # At this point we should have the fasta names and group files written out.
    # now do the size screening
    execute_size_screening(dSID=dSID, numProc=numProc, wkd=wkd, error_sample_list=error_sample_list, debug=debug)

    # Now write out the clade separated fastas from the size screened seqs
    # These will be used in MED
    write_out_clade_separated_fastas(dSID=dSID, numProc=numProc, wkd=wkd, error_sample_list=error_sample_list)

    # finally append the QC metadata for each of the samples
    associate_QC_meta_data_to_samples(dSID=dSID, numProc=numProc, wkd=wkd, error_sample_list=error_sample_list)

    # We also need to set initialDataProcessed to True
    dataSubmissionInQ.initialDataProcessed = True
    dataSubmissionInQ.save()
    if screen_sub_evalue:
        return new_seqs_added_count, discarded_seqs_fasta
    else:
        return fasta_of_sig_sub_e_seqs, fasta_of_sig_sub_e_seqs_path, discarded_seqs_fasta


def execute_worker_initial_mothur(dSID, numProc, wkd, debug):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    if not sampleFastQPairs:
        sys.exit('sample fastq pairs list empty')

    # Create the queues that will hold the sample information
    input_q = Queue()

    # list for successful sequence names
    # Some of the samples are going to fail in the initial QC and as such we cannot rely
    # on simply going to each directory and expecting there to be the set of fasta, name and group files
    # Currently we are putting the error sample names into the output_q. As such we can pass this onto the next
    # worker to determine which of the samples it still needs to be working with
    worker_manager = Manager()
    error_sample_list = worker_manager.list()

    for contigPair in sampleFastQPairs:
        input_q.put(contigPair)
    for n in range(numProc):
        input_q.put('STOP')
    all_processes = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')

    for n in range(numProc):
        p = Process(target=worker_initial_mothur, args=(input_q, error_sample_list, wkd, dSID, debug))
        all_processes.append(p)
        p.start()

    for p in all_processes:
        p.join()

    return list(error_sample_list)


def validate_taxon_screening_ref_blastdb(dataSubmissionInQ, debug):
    list_of_binaries = [dataSubmissionInQ.reference_fasta_database_used + extension for extension in
                        ['.nhr', '.nin', '.nsq']]
    sym_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
    os.chdir(sym_dir)
    list_of_dir = os.listdir(sym_dir)
    binary_count = 0
    for item in list_of_dir:
        if item in list_of_binaries:
            binary_count += 1
    if binary_count != 3:
        # then some of the binaries are not present and we need to regenerate the blast dictionary
        # generate the blast dictionary again
        if not debug:
            completed_process = subprocess.run(
                ['makeblastdb', '-in', dataSubmissionInQ.reference_fasta_database_used, '-dbtype', 'nucl', '-title',
                 dataSubmissionInQ.reference_fasta_database_used.replace('.fa', '')], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        elif debug:
            subprocess.run(
                ['makeblastdb', '-in', dataSubmissionInQ.reference_fasta_database_used, '-dbtype', 'nucl', '-title',
                 dataSubmissionInQ.reference_fasta_database_used.replace('.fa', '')])

        # now verify that the binaries have been successfully created
        list_of_dir = os.listdir(sym_dir)
        binary_count = 0
        for item in list_of_dir:
            if item in list_of_binaries:
                binary_count += 1
        if binary_count != 3:
            sys.exit('Failure in creating blast binaries')


def generate_new_stability_file_and_data_set_sample_objects(clade_list, dSID, dataSubmissionInQ, data_sheet_path,
                                                            pathToInputFile):
    # decompress (if necessary) and move the input files to the working directory
    wkd = copy_file_to_wkd(dSID, pathToInputFile)
    # Identify sample names and generate new stability file, generate data_set_sample objects in bulk
    if data_sheet_path:
        # if a data_sheet is provided ensure the samples names are derived from those in the data_sheet
        list_of_sample_objects = generate_stability_file_and_data_set_sample_objects_data_sheet(clade_list,
                                                                                                dataSubmissionInQ,
                                                                                                data_sheet_path, wkd)
    else:
        # if no data_sheet then infer the names of the samples from the .fastq.gz files
        list_of_sample_objects = generate_stability_file_and_data_set_sample_objects_inferred(clade_list,
                                                                                              dataSubmissionInQ,
                                                                                              wkd)
    # http://stackoverflow.com/questions/18383471/django-bulk-create-function-example
    smpls = data_set_sample.objects.bulk_create(list_of_sample_objects)
    return wkd, len(list_of_sample_objects)


def generate_stability_file_and_data_set_sample_objects_inferred(clade_list, dataSubmissionInQ, wkd):
    # else, we have to infer what the samples names are
    # we do this by taking off the part of the fastq.gz name that samples have in common
    end_index, list_of_names = identify_sample_names_inferred(wkd)
    # Make a batch file for mothur, set input and output dir and create a .file file
    sampleFastQPairs = generate_mothur_dotfile_file(wkd)
    newstabilityFile = []
    # if we have a data_sheet_path then we will use the sample names that the user has associated to each
    # of the fastq pairs. We will use the fastq_file_to_sample_name_dict created above to do this
    # if we do not have a data_sheet path then we will get the sample name from the first
    # fastq using the end_index that we determined above
    generate_new_stability_file_inferred(end_index, newstabilityFile, sampleFastQPairs)
    # write out the new stability file
    writeListToDestination(r'{0}/stability.files'.format(wkd), newstabilityFile)
    sampleFastQPairs = newstabilityFile
    dataSubmissionInQ.workingDirectory = wkd
    dataSubmissionInQ.save()
    # Create data_set_sample instances
    list_of_sample_objects = []
    sys.stdout.write('\nCreating data_set_sample objects\n')
    for sampleName in list_of_names:
        print('\rCreating data_set_sample {}'.format(sampleName))
        # Create the data_set_sample objects in bulk.
        # The cladalSeqTotals property of the data_set_sample object keeps track of the seq totals for the
        # sample divided by clade. This is used in the output to keep track of sequences that are not
        # included in cladeCollections
        emptyCladalSeqTotals = json.dumps([0 for cl in clade_list])

        dss = data_set_sample(name=sampleName, dataSubmissionFrom=dataSubmissionInQ,
                              cladalSeqTotals=emptyCladalSeqTotals)
        list_of_sample_objects.append(dss)
    return list_of_sample_objects


def generate_stability_file_and_data_set_sample_objects_data_sheet(clade_list, dataSubmissionInQ, data_sheet_path, wkd):
    # Create a pandas df from the data_sheet if it was provided
    sample_meta_df = pd.read_excel(io=data_sheet_path, header=0, index_col=0, usecols='A:N', skiprows=[0])
    # if we are given a data_sheet then use these sample names given as the data_set_sample object names
    fastq_file_to_sample_name_dict, list_of_names = identify_sample_names_data_sheet(sample_meta_df, wkd)
    # Make a batch file for mothur, set input and output dir and create a .file file
    sampleFastQPairs = generate_mothur_dotfile_file(wkd)
    newstabilityFile = []
    # if we have a data_sheet_path then we will use the sample names that the user has associated to each
    # of the fastq pairs. We will use the fastq_file_to_sample_name_dict created above to do this
    # if we do not have a data_sheet path then we will get the sample name from the first
    # fastq using the end_index that we determined above
    generate_new_stability_file_data_sheet(fastq_file_to_sample_name_dict, newstabilityFile, sampleFastQPairs)
    # write out the new stability file
    writeListToDestination(r'{0}/stability.files'.format(wkd), newstabilityFile)
    sampleFastQPairs = newstabilityFile
    dataSubmissionInQ.workingDirectory = wkd
    dataSubmissionInQ.save()
    # Create data_set_sample instances
    list_of_sample_objects = []
    sys.stdout.write('\nCreating data_set_sample objects\n')
    for sampleName in list_of_names:
        print('\rCreating data_set_sample {}'.format(sampleName))
        # Create the data_set_sample objects in bulk.
        # The cladalSeqTotals property of the data_set_sample object keeps track of the seq totals for the
        # sample divided by clade. This is used in the output to keep track of sequences that are not
        # included in cladeCollections
        emptyCladalSeqTotals = json.dumps([0 for cl in clade_list])

        dss = data_set_sample(name=sampleName, dataSubmissionFrom=dataSubmissionInQ,
                              cladalSeqTotals=emptyCladalSeqTotals,
                              sample_type=sample_meta_df.loc[sampleName, 'sample_type'],
                              host_phylum=sample_meta_df.loc[sampleName, 'host_phylum'],
                              host_class=sample_meta_df.loc[sampleName, 'host_class'],
                              host_order=sample_meta_df.loc[sampleName, 'host_order'],
                              host_family=sample_meta_df.loc[sampleName, 'host_family'],
                              host_genus=sample_meta_df.loc[sampleName, 'host_genus'],
                              host_species=sample_meta_df.loc[sampleName, 'host_species'],
                              collection_latitude=sample_meta_df.loc[sampleName, 'collection_latitude'],
                              collection_longitude=sample_meta_df.loc[sampleName, 'collection_longitude'],
                              collection_date=sample_meta_df.loc[sampleName, 'collection_date'],
                              collection_depth=sample_meta_df.loc[sampleName, 'collection_depth']
                              )
        list_of_sample_objects.append(dss)
    return list_of_sample_objects


def generate_new_stability_file_inferred(end_index, newstabilityFile, sampleFastQPairs):
    for stability_file_line in sampleFastQPairs:
        pairComponenets = stability_file_line.split('\t')
        # I am going to use '[dS]' as a place holder for a dash in the sample names
        # Each line of the stability file is a three column format with the first
        # column being the sample name. The second and third are the full paths of the .fastq.gz files
        # the sample name at the moment is garbage, we will extract the sample name from the
        # first fastq path using the end_index that we determined above

        newstabilityFile.append(
            '{}\t{}\t{}'.format(
                pairComponenets[1].split('/')[-1][:-end_index].replace('-', '[dS]'),
                pairComponenets[1],
                pairComponenets[2]))


def generate_new_stability_file_data_sheet(fastq_file_to_sample_name_dict, newstabilityFile, sampleFastQPairs):
    for stability_file_line in sampleFastQPairs:
        pairComponenets = stability_file_line.split('\t')
        # I am going to use '[dS]' as a place holder for a dash in the sample names
        # Each line of the stability file is a three column format with the first
        # column being the sample name. The second and third are the full paths of the .fastq.gz files
        # the sample name at the moment is garbage, we will identify the sample name from the
        # first fastq path using the fastq_file_to_sample_name_dict

        newstabilityFile.append(
            '{}\t{}\t{}'.format(
                fastq_file_to_sample_name_dict[pairComponenets[1].split('/')[-1]].replace('-', '[dS]'),
                pairComponenets[1],
                pairComponenets[2]))


def generate_mothur_dotfile_file(wkd):
    mothur_batch_file = [
        r'set.dir(input={0})'.format(wkd),
        r'set.dir(output={0})'.format(wkd),
        r'make.file(inputdir={0}, type=gz, numcols=3)'.format(wkd)
    ]
    writeListToDestination(r'{0}/mothur_batch_file_makeFile'.format(wkd), mothur_batch_file)
    completed_process = subprocess.run(['mothur', r'{0}/mothur_batch_file_makeFile'.format(wkd)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
    # Convert the group names in the stability.files so that the dashes are converted to '[ds]',
    # So for the mothur we have '[ds]'s. But for all else we convert these '[ds]'s to dashes
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    return sampleFastQPairs


def identify_sample_names_inferred(wkd):
    list_of_gz_files_in_wkd = [a for a in os.listdir(wkd) if '.gz' in a]
    # I think the simplest way to get sample names is to find what parts are common between all samples
    # well actually 50% of the samples so that we also remove the R1 and R2 parts.
    i = 1
    while 1:
        list_of_endings = []
        for file in list_of_gz_files_in_wkd:
            list_of_endings.append(file[-i:])
        if len(set(list_of_endings)) > 2:
            break
        else:
            i += 1
            # then this is one i too many and our magic i was i-1
    end_index = i - 1
    list_of_names_non_unique = []
    for file in list_of_gz_files_in_wkd:
        list_of_names_non_unique.append(file[:-end_index])
    list_of_names = list(set(list_of_names_non_unique))
    if len(list_of_names) != len(list_of_gz_files_in_wkd) / 2:
        sys.exit('Error in sample name extraction')
    return end_index, list_of_names


def identify_sample_names_data_sheet(sample_meta_df, wkd):
    # get the list of names from the index of the sample_meta_df
    list_of_names = sample_meta_df.index.values.tolist()
    # we will also need to know how to relate the samples to the fastq files
    # for this we will make a dict of fastq file name to sample
    # but before we do this we should verify that all of the fastq files listed in the sample_meta_df
    # are indeed found in the directory that we've been given
    list_of_gz_files_in_wkd = [a for a in os.listdir(wkd) if '.gz' in a]
    list_of_meta_gz_files = []
    list_of_meta_gz_files.extend(sample_meta_df['fastq_fwd_file_name'].values.tolist())
    list_of_meta_gz_files.extend(sample_meta_df['fastq_rev_file_name'].values.tolist())
    for fastq in list_of_meta_gz_files:
        if fastq not in list_of_gz_files_in_wkd:
            sys.exit('{} listed in data_sheet not found'.format(fastq, wkd))
            # todo delete the current data_submission before exiting
    # now make the dictionary
    fastq_file_to_sample_name_dict = {}
    for sample_index in sample_meta_df.index.values.tolist():
        fastq_file_to_sample_name_dict[sample_meta_df.loc[sample_index, 'fastq_fwd_file_name']] = sample_index
        fastq_file_to_sample_name_dict[sample_meta_df.loc[sample_index, 'fastq_rev_file_name']] = sample_index
    return fastq_file_to_sample_name_dict, list_of_names


def copy_file_to_wkd(dSID, pathToInputFile):
    # working directory will be housed in a temp folder within the directory in which the sequencing data
    # is currently housed
    if '.' in pathToInputFile.split('/')[-1]:
        # then this path points to a file rather than a directory and we should pass through the path only
        wkd = os.path.abspath('{}/tempData/{}'.format(os.path.dirname(pathToInputFile), dSID))
    else:
        # then we assume that we are pointing to a directory and we can directly use that to make the wkd
        wkd = os.path.abspath('{}/tempData/{}'.format(pathToInputFile, dSID))
    # if the directory already exists remove it and start from scratch
    if os.path.exists(wkd):
        shutil.rmtree(wkd)

    # create the directory that will act as the working directory for doing all of the QCing and MED in
    os.makedirs(wkd)

    # also create a directory that will be used for the pre MED QCed sequences dump
    # Within this directory we will have sample folders which will contain clade separated name and fasta pairs
    os.makedirs(wkd.replace('tempData', 'pre_MED_seqs'), exist_ok=True)

    # Check to see if the files are already decompressed
    # If so then simply copy the files over to the destination folder
    # we do this copying so that we don't corrupt the original files
    # we will delte these duplicate files after processing
    compressed = True
    for file in os.listdir(pathToInputFile):
        if 'fastq.gz' in file or 'fq.gz' in file:
            # Then there is a fastq.gz already uncompressed in this folder
            # In this case we will assume that the seq data is not compressed into a master .zip or .gz
            # Copy to the wkd
            compressed = False
            os.chdir('{}'.format(pathToInputFile))

            # * asterix are only expanded in the shell and so don't work through subprocess
            # need to use the glob library instead
            # https://stackoverflow.com/questions/13875978/python-subprocess-popen-why-does-ls-txt-not-work

            if 'fastq.gz' in file:
                completed_process = subprocess.run(['cp'] + glob.glob('*.fastq.gz') + [wkd], stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE)
            elif 'fq.gz' in file:
                completed_process = subprocess.run(['cp'] + glob.glob('*.fq.gz') + [wkd], stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE)
            break
    # if compressed then we are dealing with a single compressed file that should contain the fastq.gz pairs
    # Decompress the file to destination
    if compressed:
        extComponents = pathToInputFile.split('.')
        if extComponents[-1] == 'zip':  # .zip
            completed_process = subprocess.run(["unzip", pathToInputFile, '-d', wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
        elif extComponents[-2] == 'tar' and extComponents[-1] == 'gz':  # .tar.gz
            completed_process = subprocess.run(["tar", "-xf", pathToInputFile, "-C", wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
        elif extComponents[-1] == 'gz':  # .gz
            completed_process = subprocess.run(["gunzip", "-c", pathToInputFile, ">", wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
    return wkd


def screen_sub_e_value_sequences(ds_id, data_sub_data_dir, iteration_id, seq_sample_support_cut_off,
                                 previous_reference_fasta_name, required_symbiodinium_matches,
                                 full_path_to_nt_database_directory):
    # we need to make sure that we are looking at matches that cover > 95%
    # this is probably the most important point. We can then decide what percentage coverage we want
    # perhaps something like 60%.
    # we then need to see if there is a 'Symbiodinium' sequence that matches the query and all of these
    # requirements. If so then we consider the sequence to be Symbiodinium
    # TODO make sure that we have metrics that show how many sequences were kicked out for each iterarion that we
    # do the database update.
    # We should write out the new database with an iteration indicator so that we can keep track of the progress of the
    # database creations. We can then run the database submissions using specific iterations of the symclade dataase
    # we can name the data_set that we do so that they can link in with which database iteration they are using

    # we can work with only seuqences that are found above a certain level of support. We can use the
    # seq_sample_support_cut_off for this.

    # Write out the hidden file that points to the ncbi database directory.
    ncbircFile = []
    # db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))

    db_path = full_path_to_nt_database_directory
    ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])
    writeListToDestination("{}/.ncbirc".format(data_sub_data_dir), ncbircFile)

    # Read in the fasta files of below e values that were kicked out.
    fasta_file = readDefinedFileToList('{}/below_e_cutoff_seqs_{}.fasta'.format(data_sub_data_dir, ds_id))
    fasta_file_dict = createDictFromFasta(fasta_file)

    # screen the input fasta for sample support according to seq_sample_support_cut_off
    screened_fasta = []
    for i in range(len(fasta_file)):
        if fasta_file[i][0] == '>':
            if int(fasta_file[i].split('_')[5]) >= seq_sample_support_cut_off:
                screened_fasta.extend([fasta_file[i], fasta_file[i + 1]])

    # write out the screened fasta so that it can be read in to the blast
    # make sure to reference the sequence support and the iteration
    path_to_screened_fasta = '{}/{}_{}_{}.fasta'.format(data_sub_data_dir,
                                                        'below_e_cutoff_seqs_{}.screened'.format(ds_id), iteration_id,
                                                        seq_sample_support_cut_off)
    screened_fasta_dict = createDictFromFasta(screened_fasta)
    writeListToDestination(path_to_screened_fasta, screened_fasta)

    # Set up environment for running local blast
    blastOutputPath = r'{}/blast_{}_{}.out'.format(data_sub_data_dir, iteration_id, seq_sample_support_cut_off)
    outputFmt = "6 qseqid sseqid staxids evalue pident qcovs staxid stitle ssciname"
    # inputPath = r'{}/below_e_cutoff_seqs.fasta'.format(data_sub_data_dir)
    os.chdir(data_sub_data_dir)

    # Run local blast
    completed_process = subprocess.run(
        ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', path_to_screened_fasta, '-db', 'nt',
         '-max_target_seqs', '10', '-num_threads', '20'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Read in blast output
    blast_output_file = readDefinedFileToList(
        r'{}/blast_{}_{}.out'.format(data_sub_data_dir, iteration_id, seq_sample_support_cut_off))

    # Now go through each of the results and look to see if there is a result that has > 95% coverage and has >60%
    # match and has symbiodinium in the name.
    # if you find a number that equals the required_symbiodinium_matches
    # then add the name of this seq to the reference db

    # create a dict that is the query name key and a list of subject return value
    blast_output_dict = defaultdict(list)
    for line in blast_output_file:
        blast_output_dict[line.split('\t')[0]].append('\t'.join(line.split('\t')[1:]))

    verified_sequence_list = []
    for k, v in blast_output_dict.items():
        sym_count = 0
        for result_str in v:
            if 'Symbiodinium' in result_str:
                percentage_coverage = float(result_str.split('\t')[4])
                percentage_identity_match = float(result_str.split('\t')[3])
                if percentage_coverage > 95 and percentage_identity_match > 60:
                    sym_count += 1
                    if sym_count == required_symbiodinium_matches:
                        verified_sequence_list.append(k)
                        break

    # We only need to proceed from here to make a new database if we have sequences that ahve been verified as
    # Symbiodinium
    if verified_sequence_list:
        # here we have a list of the Symbiodinium sequences that we can add to the reference db fasta
        new_fasta = []
        for seq_to_add in verified_sequence_list:
            new_fasta.extend(['>{}'.format(seq_to_add), '{}'.format(screened_fasta_dict[seq_to_add])])

        # now add the current sequences
        previous_reference_fasta = readDefinedFileToList(
            '{}/{}'.format(os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')),
                           previous_reference_fasta_name))
        # we need to check that none of the new sequence names are found in
        new_fasta += previous_reference_fasta

        # now that the reference db fasta has had the new sequences added to it.
        # write out to the db to the database directory of SymPortal
        full_path_to_new_ref_fasta_iteration = '{}/symClade_{}_{}.fa'.format(
            os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')), iteration_id,
            seq_sample_support_cut_off)
        writeListToDestination(full_path_to_new_ref_fasta_iteration, new_fasta)

        # now update the SymPortal framework object
        symportal_framework_object = symportal_framework.objects.get(id=1)
        symportal_framework_object.latest_reference_fasta = 'symClade_{}_{}.fa'.format(iteration_id,
                                                                                       seq_sample_support_cut_off)
        symportal_framework_object.next_reference_fasta_iteration += 1
        symportal_framework_object.save()

        # run makeblastdb
        completed_process = subprocess.run(
            ['makeblastdb', '-in', full_path_to_new_ref_fasta_iteration, '-dbtype', 'nucl', '-title',
             'symClade_{}_{}'.format(iteration_id, seq_sample_support_cut_off)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        return 'symClade_{}_{}.fa'.format(iteration_id, seq_sample_support_cut_off), len(
            verified_sequence_list), full_path_to_new_ref_fasta_iteration
    else:
        return False, 0, False


def generate_sequence_drop_file():
    # this will simply produce a list
    # in the list each item will be a line of text that will be a refseq name, clade and sequence
    output_list = []
    for ref_seq in reference_sequence.objects.filter(hasName=True):
        output_list.append('{}\t{}\t{}'.format(ref_seq.name, ref_seq.clade, ref_seq.sequence))
    return output_list
