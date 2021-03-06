"""Defines the class that performs the Scale database update"""
from __future__ import unicode_literals

import logging

from django.db import transaction

from job.execution.tasks.json.results.task_results import TaskResults
from job.models import JobExecution, JobExecutionEnd, JobExecutionOutput
from util.exceptions import TerminatedCommand
from util.parse import datetime_to_string


logger = logging.getLogger(__name__)


class DatabaseUpdater(object):
    """This class manages the Scale database update. This class is thread-safe."""

    def __init__(self):
        """Constructor
        """

        self._running = True
        self._updated_job_exe = 0
        self._total_job_exe = 0

    def update(self):
        """Runs the database update
        """

        self._perform_update_init()

        while True:
            if not self._running:
                raise TerminatedCommand()

            if self._updated_job_exe >= self._total_job_exe:
                break
            self._perform_update_iteration()

    def stop(self):
        """Informs the database updater to stop running
        """

        logger.info('Scale database updater has been told to stop')
        self._running = False

    def _perform_update_init(self):
        """Performs any initialization piece of the database update
        """

        msg = 'This Scale database update converts old job_exe models into new '
        msg += 'job_exe, job_exe_end, and job_exe_output models.'
        logger.info(msg)
        logger.info('Counting the number of job executions that need to be updated...')
        self._total_job_exe = JobExecution.objects.filter(status__isnull=False).count()
        logger.info('Found %d job executions that need to be updated', self._total_job_exe)

    def _perform_update_iteration(self):
        """Performs a single iteration of the database update
        """

        # Retrieve 500 job executions that need to be updated and get job IDs
        job_ids = set()
        for job_exe in JobExecution.objects.filter(status__isnull=False).only('id', 'job_id')[:500]:
            job_ids.add(job_exe.job_id)

        # Retrieve all job executions for those jobs in sorted order
        job_exe_count = 0
        current_job_id = None
        current_exe_num = 1
        exe_num_dict = {}  # {exe_num: [job_exe.id]}
        job_exe_end_models = []
        job_exe_output_models = []
        job_exe_qry = JobExecution.objects.select_related('job').filter(job_id__in=job_ids)
        for job_exe in job_exe_qry.defer('resources', 'configuration', 'stdout', 'stderr').order_by('job_id', 'id'):
            job_exe_count += 1
            if job_exe.job_id == current_job_id:
                current_exe_num += 1
            else:
                current_job_id = job_exe.job_id
                current_exe_num = 1

            # This job_exe model needs to be updated with its exe_num
            if current_exe_num in exe_num_dict:
                exe_num_dict[current_exe_num].append(job_exe.id)
            else:
                exe_num_dict[current_exe_num] = [job_exe.id]

            if job_exe.status in ['COMPLETED', 'FAILED', 'CANCELED']:
                # Create corresponding job_exe_end model
                job_exe_end = JobExecutionEnd()
                job_exe_end.job_exe_id = job_exe.id
                job_exe_end.job_id = job_exe.job_id
                job_exe_end.job_type_id = job_exe.job.job_type_id
                job_exe_end.exe_num = current_exe_num

                # Create task results from job_exe task fields
                task_list = []
                if job_exe.pre_started:
                    pre_task_dict = {'task_id': '%s_%s' % (job_exe.get_cluster_id(), 'pre'), 'type': 'pre',
                                     'was_launched': True, 'was_started': True,
                                     'started': datetime_to_string(job_exe.pre_started)}
                    if job_exe.pre_completed:
                        pre_task_dict['ended'] = datetime_to_string(job_exe.pre_completed)
                    if job_exe.pre_exit_code is not None:
                        pre_task_dict['exit_code'] = job_exe.pre_exit_code
                    task_list.append(pre_task_dict)
                if job_exe.job_started:
                    job_task_dict = {'task_id': '%s_%s' % (job_exe.get_cluster_id(), 'job'), 'type': 'main',
                                     'was_launched': True, 'was_started': True,
                                     'started': datetime_to_string(job_exe.job_started)}
                    if job_exe.job_completed:
                        job_task_dict['ended'] = datetime_to_string(job_exe.job_completed)
                    if job_exe.job_exit_code is not None:
                        job_task_dict['exit_code'] = job_exe.job_exit_code
                    task_list.append(job_task_dict)
                if job_exe.post_started:
                    post_task_dict = {'task_id': '%s_%s' % (job_exe.get_cluster_id(), 'post'), 'type': 'post',
                                      'was_launched': True, 'was_started': True,
                                      'started': datetime_to_string(job_exe.post_started)}
                    if job_exe.post_completed:
                        post_task_dict['ended'] = datetime_to_string(job_exe.post_completed)
                    if job_exe.post_exit_code is not None:
                        post_task_dict['exit_code'] = job_exe.post_exit_code
                    task_list.append(post_task_dict)
                task_results = TaskResults({'tasks': task_list})

                job_exe_end.task_results = task_results.get_dict()
                job_exe_end.status = job_exe.status
                job_exe_end.error_id = job_exe.error_id
                job_exe_end.node_id = job_exe.node_id
                job_exe_end.queued = job_exe.queued
                job_exe_end.started = job_exe.started
                job_exe_end.ended = job_exe.ended
                job_exe_end_models.append(job_exe_end)

            if job_exe.status == 'COMPLETED':
                # Create corresponding job_exe_output model
                job_exe_output = JobExecutionOutput()
                job_exe_output.job_exe_id = job_exe.id
                job_exe_output.job_id = job_exe.job_id
                job_exe_output.job_type_id = job_exe.job.job_type_id
                job_exe_output.exe_num = current_exe_num
                job_exe_output.output = job_exe.results
                job_exe_output_models.append(job_exe_output)

        # Update/create models in an atomic transaction
        with transaction.atomic():
            for exe_num, job_exe_ids in exe_num_dict.items():
                JobExecution.objects.filter(id__in=job_exe_ids).update(exe_num=exe_num, status=None, error_id=None,
                                                                       command_arguments=None, environment=None,
                                                                       cpus_scheduled=None, mem_scheduled=None,
                                                                       disk_out_scheduled=None,
                                                                       disk_total_scheduled=None, pre_started=None,
                                                                       pre_completed=None, pre_exit_code=None,
                                                                       job_started=None, job_completed=None,
                                                                       job_exit_code=None, job_metrics=None,
                                                                       post_started=None, post_completed=None,
                                                                       post_exit_code=None, stdout=None, stderr=None,
                                                                       results_manifest=None, results=None, ended=None,
                                                                       last_modified=None)
            JobExecutionEnd.objects.bulk_create(job_exe_end_models)
            JobExecutionOutput.objects.bulk_create(job_exe_output_models)

        logger.info('Updated %d job executions', job_exe_count)
        self._updated_job_exe += job_exe_count
        percent = (float(self._updated_job_exe) / float(self._total_job_exe)) * 100.00
        print 'Completed %s of %s job executions (%.1f%%)' % (self._updated_job_exe, self._total_job_exe, percent)
