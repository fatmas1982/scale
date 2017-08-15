"""Defines the database models for jobs and job types"""
from __future__ import unicode_literals

import copy
import datetime
import logging
import math

import django.contrib.postgres.fields
import django.utils.html
from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

import util.parse
from error.models import Error
from job.configuration.data.job_data import JobData
from job.configuration.interface.error_interface import ErrorInterface
from job.configuration.interface.job_interface import JobInterface
from job.configuration.json.execution.exe_config import ExecutionConfiguration
from job.configuration.json.job.job_config import JobConfiguration
from job.configuration.results.job_results import JobResults
from job.exceptions import InvalidJobField
from job.execution.tasks.exe_task import JOB_TASK_ID_PREFIX
from job.triggers.configuration.trigger_rule import JobTriggerRuleConfiguration
from node.resources.json.resources import Resources
from node.resources.node_resources import NodeResources
from node.resources.resource import Cpus, Disk, Mem
from storage.models import ScaleFile
from trigger.configuration.exceptions import InvalidTriggerType
from trigger.models import TriggerRule
from util.exceptions import RollbackTransaction
from vault.secrets_handler import SecretsHandler


logger = logging.getLogger(__name__)


# Required resource minimums for jobs (e.g. resources required for pre and post tasks)
MIN_CPUS = 0.25
MIN_MEM = 128.0
MIN_DISK = 0.0


# IMPORTANT NOTE: Locking order
# Always adhere to the following model order for obtaining row locks via select_for_update() in order to prevent
# deadlocks and ensure query efficiency
# When applying status updates to jobs: JobExecution, Queue, Job, Recipe
# When editing a job/recipe type: RecipeType, JobType, TriggerRule


class JobManager(models.Manager):
    """Provides additional methods for handling jobs
    """

    def complete_job(self, job, when):
        """Updates the given job to the COMPLETED status. The caller must have obtained the job model's lock. All
        database updates occur in an atomic transaction.

        :param job: The job model
        :type job: :class:`job.models.Job`
        :param when: The completed time
        :type when: :class:`datetime.datetime`
        """

        job.status = 'COMPLETED'
        job.ended = when
        job.last_status_change = when

        # Query output from completed job execution
        job_exe_output = JobExecutionOutput.objects.get(job_id=job.id, exe_num=job.exe_num)
        job.results = job_exe_output.get_output().get_dict()

        job.save()

        # Update completed job count if part of a batch
        from batch.models import Batch, BatchJob
        try:
            batch_job = BatchJob.objects.get(job_id=job.id)
            Batch.objects.count_completed_job(batch_job.batch.id)
        except BatchJob.DoesNotExist:
            pass

    def create_job(self, job_type, event, superseded_job=None, delete_superseded=True):
        """Creates a new job for the given type and returns the job model. Optionally a job can be provided that the new
        job is superseding. If provided, the caller must have obtained a model lock on the job to supersede. The
        returned job model will have not yet been saved in the database.

        :param job_type: The type of the job to create
        :type job_type: :class:`job.models.JobType`
        :param event: The event that triggered the creation of this job
        :type event: :class:`trigger.models.TriggerEvent`
        :param superseded_job: The job that the created job is superseding, possibly None
        :type superseded_job: :class:`job.models.Job`
        :param delete_superseded: Whether the created job should delete products from the superseded job
        :type delete_superseded: :class:`job.models.Job`
        :returns: The new job
        :rtype: :class:`job.models.Job`
        """
        if not job_type.is_active:
            raise Exception('Job type is no longer active')
        if event is None:
            raise Exception('Event that triggered job creation is required')

        job = Job()
        job.job_type = job_type
        job.job_type_rev = JobTypeRevision.objects.get_revision(job_type.id, job_type.revision_num)
        job.event = event
        job.priority = job_type.priority
        job.timeout = job_type.timeout
        job.max_tries = job_type.max_tries

        if superseded_job:
            root_id = superseded_job.root_superseded_job_id
            if not root_id:
                root_id = superseded_job.id
            job.root_superseded_job_id = root_id
            job.superseded_job = superseded_job
            job.delete_superseded = delete_superseded

        return job

    def fail_job(self, job, when, error):
        """Updates the given job to the FAILED status. The caller must have obtained the job model's lock. All database
        updates occur in an atomic transaction.

        :param job: The job model
        :type job: :class:`job.models.Job`
        :param when: The completed time
        :type when: :class:`datetime.datetime`
        :param error: The error that caused the failure
        :type error: :class:`error.models.Error`
        """

        job.status = 'FAILED'
        job.error = error
        job.ended = when
        job.last_status_change = when
        job.save()

    def filter_jobs(self, started=None, ended=None, statuses=None, job_ids=None, job_type_ids=None, job_type_names=None,
                    job_type_categories=None, batch_ids=None, error_categories=None, include_superseded=False, 
                    order=None):
        """Returns a query for job models that filters on the given fields. The returned query includes the related
        job_type, job_type_rev, event, and error fields, except for the job_type.interface and job_type_rev.interface
        fields.

        :param started: Query jobs updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query jobs updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param statuses: Query jobs with the a specific execution status.
        :type statuses: [string]
        :param job_ids: Query jobs associated with the identifier.
        :type job_ids: [int]
        :param job_type_ids: Query jobs of the type associated with the identifier.
        :type job_type_ids: [int]
        :param job_type_names: Query jobs of the type associated with the name.
        :type job_type_names: [string]
        :param job_type_categories: Query jobs of the type associated with the category.
        :type job_type_categories: [string]
        :param batch_ids: Query jobs associated with the given batch identifiers.
        :type batch_ids: list[int]
        :param error_categories: Query jobs that failed due to errors associated with the category.
        :type error_categories: [string]
        :param include_superseded: Whether to include jobs that are superseded.
        :type include_superseded: bool
        :param order: A list of fields to control the sort order.
        :type order: [string]
        :returns: The job query
        :rtype: :class:`django.db.models.QuerySet`
        """

        # Fetch a list of jobs
        jobs = Job.objects.all().select_related('job_type', 'job_type_rev', 'event', 'error')
        jobs = jobs.defer('job_type__interface', 'job_type_rev__job_type', 'job_type_rev__interface')

        # Apply time range filtering
        if started:
            jobs = jobs.filter(last_modified__gte=started)
        if ended:
            jobs = jobs.filter(last_modified__lte=ended)

        # Apply additional filters
        if statuses:
            jobs = jobs.filter(status__in=statuses)
        if job_ids:
            jobs = jobs.filter(id__in=job_ids)
        if job_type_ids:
            jobs = jobs.filter(job_type_id__in=job_type_ids)
        if job_type_names:
            jobs = jobs.filter(job_type__name__in=job_type_names)
        if job_type_categories:
            jobs = jobs.filter(job_type__category__in=job_type_categories)
        if batch_ids:
            jobs = jobs.filter(batchjob__batch__in=batch_ids)
        if error_categories:
            jobs = jobs.filter(error__category__in=error_categories)
        if not include_superseded:
            jobs = jobs.filter(is_superseded=False)

        # Apply sorting
        if order:
            jobs = jobs.order_by(*order)
        else:
            jobs = jobs.order_by('last_modified')
        return jobs

    def get_jobs(self, started=None, ended=None, statuses=None, job_ids=None, job_type_ids=None, job_type_names=None,
                 job_type_categories=None, batch_ids=None, error_categories=None, include_superseded=False, 
                 order=None):
        """Returns a list of jobs within the given time range.

        :param started: Query jobs updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query jobs updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param statuses: Query jobs with the a specific execution status.
        :type statuses: [string]
        :param job_ids: Query jobs associated with the identifier.
        :type job_ids: [int]
        :param job_type_ids: Query jobs of the type associated with the identifier.
        :type job_type_ids: [int]
        :param job_type_names: Query jobs of the type associated with the name.
        :type job_type_names: [string]
        :param job_type_categories: Query jobs of the type associated with the category.
        :type job_type_categories: [string]
        :param batch_ids: Query jobs associated with batches with the given identifiers.
        :type batch_ids: list[int]
        :param error_categories: Query jobs that failed due to errors associated with the category.
        :type error_categories: [string]
        :param include_superseded: Whether to include jobs that are superseded.
        :type include_superseded: bool
        :param order: A list of fields to control the sort order.
        :type order: [string]
        :returns: The list of jobs that match the time range.
        :rtype: [:class:`job.models.Job`]
        """

        return self.filter_jobs(started=started, ended=ended, statuses=statuses, job_ids=job_ids,
                                job_type_ids=job_type_ids, job_type_names=job_type_names,
                                job_type_categories=job_type_categories, batch_ids=batch_ids, 
                                error_categories=error_categories, include_superseded=include_superseded, 
                                order=order)

    def get_details(self, job_id):
        """Gets additional details for the given job model based on related model attributes.

        The additional fields include: input files, recipe, job executions, and generated products.

        :param job_id: The unique identifier of the job.
        :type job_id: int
        :returns: The job with extra related attributes.
        :rtype: :class:`job.models.Job`
        """

        # Attempt to fetch the requested job
        job = Job.objects.select_related(
            'job_type', 'job_type_rev', 'job_type_rev__job_type', 'event', 'event__rule', 'error',
            'root_superseded_job', 'root_superseded_job__job_type', 'superseded_job', 'superseded_job__job_type',
            'superseded_by_job', 'superseded_by_job__job_type'
        ).get(pk=job_id)

        # Attempt to get related job executions
        job_exes = JobExecution.objects.filter(job=job).select_related('job', 'node', 'error')
        job.job_exes = job_exes.defer('job__data', 'job__configuration', 'job__results').order_by('-created')

        # Attempt to get related recipe
        # Use a localized import to make higher level application dependencies optional
        try:
            from recipe.models import RecipeJob
            recipe_jobs = RecipeJob.objects.filter(job=job).order_by('recipe__last_modified')
            recipe_jobs = recipe_jobs.select_related('recipe', 'recipe__recipe_type', 'recipe__recipe_type_rev',
                                                     'recipe__recipe_type_rev__recipe_type', 'recipe__event',
                                                     'recipe__event__rule')
            job.recipes = [recipe_job.recipe for recipe_job in recipe_jobs]
        except:
            job.recipes = []

        # Fetch all the associated input files
        input_file_ids = job.get_job_data().get_input_file_ids()
        input_files = ScaleFile.objects.filter(id__in=input_file_ids)
        input_files = input_files.select_related('workspace', 'job_type', 'job', 'job_exe')
        input_files = input_files.defer('workspace__json_config', 'job__data', 'job__configuration', 'job__results',
                                        'job_exe__environment', 'job_exe__configuration', 'job_exe__job_metrics',
                                        'job_exe__stdout', 'job_exe__stderr', 'job_exe__results',
                                        'job_exe__results_manifest', 'job_type__interface', 'job_type__docker_params',
                                        'job_type__configuration', 'job_type__error_mapping')
        input_files = input_files.prefetch_related('countries')
        input_files = input_files.order_by('id').distinct('id')

        # Attempt to get related products
        output_files = ScaleFile.objects.filter(job=job)
        output_files = output_files.select_related('workspace', 'job_type', 'job', 'job_exe')
        output_files = output_files.defer('workspace__json_config', 'job__data', 'job__configuration', 'job__results',
                                          'job_exe__environment', 'job_exe__configuration', 'job_exe__job_metrics',
                                          'job_exe__stdout', 'job_exe__stderr', 'job_exe__results',
                                          'job_exe__results_manifest', 'job_type__interface', 'job_type__docker_params',
                                          'job_type__configuration', 'job_type__error_mapping')
        output_files = output_files.prefetch_related('countries')
        output_files = output_files.order_by('id').distinct('id')

        # Merge job interface definitions with mapped values
        job_interface_dict = job.get_job_interface().get_dict()
        job_data_dict = job.get_job_data().get_dict()
        job_results_dict = job.get_job_results().get_dict()
        job.inputs = self._merge_job_data(job_interface_dict['input_data'], job_data_dict['input_data'], input_files)
        job.outputs = self._merge_job_data(job_interface_dict['output_data'], job_results_dict['output_data'],
                                           output_files)

        return job

    def get_job_updates(self, started=None, ended=None, statuses=None, job_type_ids=None,
                        job_type_names=None, job_type_categories=None, include_superseded=False, order=None):
        """Returns a list of jobs that changed status within the given time range.

        :param started: Query jobs updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query jobs updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param statuses: Query jobs with the a specific execution status.
        :type statuses: [string]
        :param job_type_ids: Query jobs of the type associated with the identifier.
        :type job_type_ids: [int]
        :param job_type_categories: Query jobs of the type associated with the category.
        :type job_type_categories: [string]
        :param job_type_names: Query jobs of the type associated with the name.
        :type job_type_names: [string]
        :param include_superseded: Whether to include jobs that are superseded.
        :type include_superseded: bool
        :param order: A list of fields to control the sort order.
        :type order: [string]
        :returns: The list of jobs that match the time range.
        :rtype: [:class:`job.models.Job`]
        """
        if not order:
            order = ['last_status_change']
        return self.get_jobs(started=started, ended=ended, statuses=statuses, job_type_ids=job_type_ids,
                             job_type_names=job_type_names, job_type_categories=job_type_categories,
                             include_superseded=include_superseded, order=order)

    def get_locked_job(self, job_id):
        """Locks and returns the job model for the given ID with no related fields. Caller must be within an atomic
        transaction.

        :param job_id: The job ID
        :type job_id: int
        :returns: The job model
        :rtype: :class:`job.models.Job`
        """

        return self.get_locked_jobs([job_id])[0]

    def get_locked_jobs(self, job_ids):
        """Locks and returns the job models for the given IDs with no related fields. Caller must be within an atomic
        transaction.

        :param job_ids: The job IDs
        :type job_ids: list
        :returns: The job models
        :rtype: list
        """

        # Job models are always locked in order of ascending ID to prevent deadlocks
        return list(self.select_for_update().filter(id__in=job_ids).order_by('id').iterator())

    # TODO: might be able to remove this
    def get_locked_jobs_with_related(self, job_ids):
        """Gets the job models for the given IDs with model locks obtained and related job_type and job_type_rev models

        :param job_ids: The job IDs
        :type job_ids: [int]
        :returns: The job models
        :rtype: [:class:`job.models.Job`]
        """

        self.lock_jobs(job_ids)

        return list(self.select_related('job_type', 'job_type_rev').filter(id__in=job_ids).iterator())

    def increment_max_tries(self, jobs):
        """Increments the max_tries of the given jobs to be one greater than their current number of executions. The
        caller must have obtained model locks on the job models.

        :param jobs: The jobs to update
        :type jobs: [:class:`job.models.Job`]
        """

        modified = timezone.now()

        # Update job models in memory and collect job IDs
        job_ids = set()
        for job in jobs:
            job_ids.add(job.id)
            job.max_tries = job.num_exes + 1
            job.last_modified = modified

        # Update job models in database with single query
        self.filter(id__in=job_ids).update(max_tries=models.F('num_exes') + 1, last_modified=modified)

    def lock_jobs(self, job_ids):
        """Obtains model locks on the job models with the given IDs (in ID order to prevent deadlocks)

        :param job_ids: The IDs of the jobs to lock
        :type job_ids: [int]
        """

        # Dummy list is used here to force query execution
        # Unfortunately this query can't usually be combined with other queries since using select_related() with
        # select_for_update() will cause the related fields to be locked as well. This requires 2 passes, such as the
        # two queries in get_locked_jobs_with_related().
        list(self.select_for_update().filter(id__in=job_ids).order_by('id').iterator())

    def queue_jobs(self, jobs, when, priority=None):
        """Queues the given jobs and returns the models that are successfully set to QUEUED. The caller must have
        obtained model locks on the job models in an atomic transaction. Any jobs that are not in a valid status for
        being queued, are without job data, or are superseded will be ignored. All jobs should have their related
        job_type and job_type_rev models populated.

        :param jobs: The job models to set to QUEUED
        :type jobs: list
        :param when: The time that the jobs are queued
        :type when: :class:`datetime.datetime`
        :param priority: An optional argument to reset the jobs' priority before they are queued
        :type priority: int
        :returns: The list of job models that were successfully set to QUEUED
        :rtype: list
        """

        # Update job models in memory and collect job IDs
        job_ids = set()
        jobs_to_queue = []
        for job in jobs:
            if not job.is_ready_to_queue or not job.data or job.is_superseded:
                continue

            job_ids.add(job.id)
            jobs_to_queue.append(job)
            job.status = 'QUEUED'
            job.error = None
            job.queued = when
            job.started = None
            job.ended = None
            job.last_status_change = when
            job.num_exes += 1
            if priority:
                job.priority = priority
            job.last_modified = when

        # Update job models in database with single query
        if priority:
            self.filter(id__in=job_ids).update(status='QUEUED', error=None, queued=when, started=None, ended=None,
                                               last_status_change=when, num_exes=models.F('num_exes') + 1,
                                               priority=priority, last_modified=when)
        else:
            self.filter(id__in=job_ids).update(status='QUEUED', error=None, queued=when, started=None, ended=None,
                                               last_status_change=when, num_exes=models.F('num_exes') + 1,
                                               last_modified=when)

        return jobs_to_queue

    def populate_job_data(self, job, data):
        """Populates the job data and all derived fields for the given job. The caller must have obtained a model lock
        on the job model. The job should have its related job_type and job_type_rev models populated.

        :param job: The job
        :type job: :class:`job.models.Job`
        :param data: JSON description defining the job data to run on
        :type data: :class:`job.configuration.data.job_data.JobData`
        :raises job.configuration.data.exceptions.InvalidData: If the job data is invalid
        """

        modified = timezone.now()

        # Validate job data
        interface = job.get_job_interface()
        interface.validate_data(data)

        # Analyze input files
        input_file_ids = data.get_input_file_ids()
        input_files = ScaleFile.objects.get_files(input_file_ids)
        input_size_bytes = 0
        for input_file in input_files:
            input_size_bytes += input_file.file_size

        # Calculate total input file size in MiB rounded up to the nearest whole MiB
        input_size_mb = long(math.ceil((input_size_bytes / (1024.0 * 1024.0))))
        # Calculate output space required in MiB rounded up to the nearest whole MiB
        multiplier = job.job_type.disk_out_mult_required
        const = job.job_type.disk_out_const_required
        output_size_mb = long(math.ceil(multiplier * input_size_mb + const))
        disk_in_required = max(input_size_mb, MIN_DISK)
        disk_out_required = max(output_size_mb, MIN_DISK)

        # Update job model in memory
        job.data = data.get_dict()
        job.disk_in_required = disk_in_required
        job.disk_out_required = disk_out_required
        job.last_modified = modified

        # Configure and populate JobInputFile
        job_inputs = []
        for input_file in data.get_input_file_info():
            job_input = JobInputFile()
            job_input.job_id = job.id
            job_input.input_file_id = input_file[0]
            job_input.job_input = input_file[1]
            job_inputs.append(job_input)
        JobInputFile.objects.bulk_create(job_inputs)

        # Populate file ancestry links
        from product.models import FileAncestryLink
        FileAncestryLink.objects.create_file_ancestry_links(input_file_ids, None, job, None)

        # Update job model in database with single query
        self.filter(id=job.id).update(data=data.get_dict(), disk_in_required=disk_in_required,
                                      disk_out_required=disk_out_required, last_modified=modified)

    def populate_input_files(self, jobs):
        """Populates each of the given jobs with its input file references in a field called "input_files".

        :param jobs: The list of jobs to augment with input files.
        :type jobs: [:class:`job.models.Job`]
        """

        # Build a unique set of all input file identifiers
        # Build a mapping of job to its input file identifiers
        file_ids = set()
        job_file_map = dict()
        for job in jobs:
            input_file_ids = job.get_job_data().get_input_file_ids()
            job_file_map[job.id] = input_file_ids
            file_ids.update(input_file_ids)
            job.input_files = []

        # Fetch all the required source files
        input_files = ScaleFile.objects.filter(id__in=file_ids)
        input_files = input_files.select_related('workspace').defer('workspace__json_config')
        input_files = input_files.order_by('id').distinct('id')

        # Build a mapping of input file identifiers to input file
        input_file_map = {input_file.id: input_file for input_file in input_files}

        # Update each job with source file models
        for job in jobs:
            input_file_ids = job_file_map[job.id]
            for input_file_id in input_file_ids:
                if input_file_id in input_file_map:
                    job.input_files.append(input_file_map[input_file_id])

    def supersede_jobs(self, jobs, when):
        """Updates the given jobs to be superseded. The caller must have obtained model locks on the job models.

        :param jobs: The jobs to supersede
        :type jobs: [:class:`job.models.Job`]
        :param when: The time that the jobs were superseded
        :type when: :class:`datetime.datetime`
        """

        modified = timezone.now()

        # Update job models in memory and collect job IDs
        job_ids = set()
        jobs_to_cancel = []
        for job in jobs:
            job_ids.add(job.id)
            job.is_superseded = True
            job.superseded = when
            job.last_modified = modified
            if job.status in ['PENDING', 'BLOCKED']:
                jobs_to_cancel.append(job)

        # Update job models in database with single query
        self.filter(id__in=job_ids).update(is_superseded=True, superseded=when, last_modified=modified)

        # Cancel any jobs that are PENDING or BLOCKED
        if jobs_to_cancel:
            self.update_status(jobs_to_cancel, 'CANCELED', when)

    @transaction.atomic
    def update_jobs_to_canceled(self, job_ids, when):
        """Updates the given jobs to the CANCELED status. Any jobs that cannot be canceled will be ignored. All database
        updates occur in an atomic transaction.

        :param job_ids: The list of job IDs
        :type job_ids: list
        :param when: The cancel time
        :type when: :class:`datetime.datetime`
        """

        jobs_to_update = []
        for locked_job in self.get_locked_jobs(job_ids):
            if locked_job.can_be_canceled:
                jobs_to_update.append(locked_job.id)

        if jobs_to_update:
            # Update job models in database
            self.filter(id__in=jobs_to_update).update(status='CANCELED', last_status_change=when,
                                                      last_modified=timezone.now())

    @transaction.atomic
    def update_jobs_to_running(self, jobs, when):
        """Updates the given jobs to the RUNNING status. The number of each job's running execution is provided to
        resolve race conditions. All database updates occur in an atomic transaction.

        :param jobs: A dict where each job ID maps to its running execution number
        :type jobs: dict
        :param when: The start time
        :type when: :class:`datetime.datetime`
        """

        jobs_to_update = []
        jobs_to_update_no_status = []  # These are jobs that need to be updated, but without a status change
        for locked_job in self.get_locked_jobs(jobs.keys()):
            exe_num = jobs[locked_job.exe_num]
            if locked_job.exe_num != exe_num:
                # If the execution number has changed, this update is obsolete
                continue
            if locked_job.exe_num.status == 'QUEUED':
                jobs_to_update.append(locked_job.id)
            else:
                # The job has already received its final status update, don't update status
                jobs_to_update_no_status.append(locked_job.id)

        modified = timezone.now()
        if jobs_to_update:
            # Update job models in database
            self.filter(id__in=jobs_to_update).update(status='RUNNING', last_status_change=when, started=when,
                                                      last_modified=modified)
        if jobs_to_update_no_status:
            # Update job models in database except for status change
            self.filter(id__in=jobs_to_update_no_status).update(started=when, last_modified=modified)

    def update_status(self, jobs, status, when, error=None):
        """Updates the given jobs with the new status. The caller must have obtained model locks on the job models.

        :param jobs: The jobs to update
        :type jobs: [:class:`job.models.Job`]
        :param status: The new status
        :type status: string
        :param when: The time that the status change occurred
        :type when: :class:`datetime.datetime`
        :param error: The error that caused the failure (required if status is FAILED, should be None otherwise)
        :type error: :class:`error.models.Error`
        """

        if status == 'QUEUED':
            raise Exception('Changing status to QUEUED must use the queue_jobs() method')
        if status == 'FAILED' and not error:
            raise Exception('An error is required when status is FAILED')
        if not status == 'FAILED' and error:
            raise Exception('Status %s is invalid with an error' % status)

        change_started = (status == 'RUNNING')
        ended = when if status in Job.FINAL_STATUSES else None
        modified = timezone.now()

        # Update job models in memory and collect job IDs
        job_ids = set()
        for job in jobs:
            job_ids.add(job.id)
            job.status = status
            job.last_status_change = when
            if change_started:
                job.started = when
            job.ended = ended
            job.error = error
            job.last_modified = modified

        # Update job models in database with single query
        if change_started:
            self.filter(id__in=job_ids).update(status=status, last_status_change=when, started=when, ended=ended,
                                               error=error, last_modified=modified)
        else:
            self.filter(id__in=job_ids).update(status=status, last_status_change=when, ended=ended, error=error,
                                               last_modified=modified)

    def _merge_job_data(self, job_interface_dict, job_data_dict, job_files):
        """Merges data for a single job instance with its job interface to produce a mapping of key/values.

        :param job_interface_dict: A dictionary representation of the job type interface.
        :type job_interface_dict: dict
        :param job_data_dict: A dictionary representation of the job instance data.
        :type job_data_dict: dict
        :param job_files: A list of files that are referenced by the job data.
        :type job_files: [:class:`storage.models.ScaleFile`]
        :return: A dictionary of each interface key mapped to the corresponding data value.
        :rtype: dict
        """

        # Setup the basic structure for merged results
        merged_dicts = copy.deepcopy(job_interface_dict)
        name_map = {merged_dict['name']: merged_dict for merged_dict in merged_dicts}
        file_map = {job_file.id: job_file for job_file in job_files}

        # Merge the job data with the interface attributes
        for data_dict in job_data_dict:
            value = None
            if 'value' in data_dict:
                value = data_dict['value']
            elif 'file_id' in data_dict:
                value = file_map[data_dict['file_id']]
            elif 'file_ids' in data_dict:
                value = [file_map[file_id] for file_id in data_dict['file_ids']]

            name = data_dict['name']
            if name in name_map:
                merged_dict = name_map[name]
                merged_dict['value'] = value
        return merged_dicts


class Job(models.Model):
    """Represents a job to be run on the cluster. Any status updates to a job model requires obtaining a lock on the
    model using select_for_update(). If a related job execution model is changing status as well, its model lock must be
    obtained BEFORE obtaining the lock for the job model.

    :keyword job_type: The type of this job
    :type job_type: :class:`django.db.models.ForeignKey`
    :keyword job_type_rev: The revision of the job type when this job was created
    :type job_type_rev: :class:`django.db.models.ForeignKey`
    :keyword status: The status of the job
    :type status: :class:`django.db.models.CharField`
    :keyword event: The event that triggered the creation of this job
    :type event: :class:`django.db.models.ForeignKey`
    :keyword error: The error that caused the failure (should only be set when status is FAILED)
    :type error: :class:`django.db.models.ForeignKey`

    :keyword data: JSON description defining the data for this job. This field must be populated when the job is first
        queued.
    :type data: :class:`django.contrib.postgres.fields.JSONField`
    :keyword results: JSON description defining the results for this job. This field is populated when the job is
        successfully completed.
    :type results: :class:`django.contrib.postgres.fields.JSONField`

    :keyword priority: The priority of the job (lower number is higher priority)
    :type priority: :class:`django.db.models.IntegerField`
    :keyword timeout: The maximum amount of time to allow this job to run before being killed (in seconds)
    :type timeout: :class:`django.db.models.IntegerField`
    :keyword max_tries: The maximum number of times to try executing this job in case of errors (minimum one)
    :type max_tries: :class:`django.db.models.IntegerField`
    :keyword num_exes: The number of executions this job has had
    :type num_exes: :class:`django.db.models.IntegerField`
    :keyword cpus_required: The number of CPUs required for this job
    :type cpus_required: :class:`django.db.models.FloatField`
    :keyword mem_required: The amount of RAM in MiB required for this job
    :type mem_required: :class:`django.db.models.FloatField`
    :keyword disk_in_required: The amount of disk space in MiB required for input files for this job
    :type disk_in_required: :class:`django.db.models.FloatField`
    :keyword disk_out_required: The amount of disk space in MiB required for output (temp work and products) for this
        job
    :type disk_out_required: :class:`django.db.models.FloatField`

    :keyword is_superseded: Whether this job has been superseded and is obsolete. This may be true while
        superseded_by_job (the reverse relationship of superseded_job) is null, indicating that this job is obsolete
        (its recipe has been superseded), but there is no new job that has directly taken its place.
    :type is_superseded: :class:`django.db.models.BooleanField`
    :keyword root_superseded_job: The first job in the chain of superseded jobs. This field will be null for the first
        job in the chain (i.e. jobs that have a null superseded_job field).
    :type root_superseded_job: :class:`django.db.models.ForeignKey`
    :keyword superseded_job: The job that was directly superseded by this job. The reverse relationship can be accessed
        using 'superseded_by_job'.
    :type superseded_job: :class:`django.db.models.ForeignKey`
    :keyword delete_superseded: Whether this job should delete the products of the job that it has directly superseded
    :type delete_superseded: :class:`django.db.models.BooleanField`

    :keyword created: When the job was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword queued: When the job was added to the queue to be run when resources are available
    :type queued: :class:`django.db.models.DateTimeField`
    :keyword started: When the job started running
    :type started: :class:`django.db.models.DateTimeField`
    :keyword ended: When the job stopped running, which could be due to successful completion or an error condition
    :type ended: :class:`django.db.models.DateTimeField`
    :keyword last_status_change: When the job's last status change occurred
    :type last_status_change: :class:`django.db.models.DateTimeField`
    :keyword superseded: When this job was superseded
    :type superseded: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the job was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    """

    JOB_STATUSES = (
        ('PENDING', 'PENDING'),
        ('BLOCKED', 'BLOCKED'),
        ('QUEUED', 'QUEUED'),
        ('RUNNING', 'RUNNING'),
        ('FAILED', 'FAILED'),
        ('COMPLETED', 'COMPLETED'),
        ('CANCELED', 'CANCELED'),
    )
    FINAL_STATUSES = ['FAILED', 'COMPLETED', 'CANCELED']

    job_type = models.ForeignKey('job.JobType', on_delete=models.PROTECT)
    job_type_rev = models.ForeignKey('job.JobTypeRevision', on_delete=models.PROTECT)
    status = models.CharField(choices=JOB_STATUSES, default='PENDING', max_length=50, db_index=True)
    event = models.ForeignKey('trigger.TriggerEvent', on_delete=models.PROTECT)
    error = models.ForeignKey('error.Error', blank=True, null=True, on_delete=models.PROTECT)

    data = django.contrib.postgres.fields.JSONField(default=dict)
    results = django.contrib.postgres.fields.JSONField(default=dict)

    priority = models.IntegerField()
    timeout = models.IntegerField()
    max_tries = models.IntegerField()
    num_exes = models.IntegerField(default=0)
    # TODO: remove cpus_required, mem_required, and disk_out_required, will cause breaking REST API changes
    # TODO: rename disk_in_required to input_file_size, will cause breaking REST API changes
    cpus_required = models.FloatField(blank=True, null=True)
    mem_required = models.FloatField(blank=True, null=True)
    disk_in_required = models.FloatField(blank=True, null=True)
    disk_out_required = models.FloatField(blank=True, null=True)

    is_superseded = models.BooleanField(default=False)
    root_superseded_job = models.ForeignKey('job.Job', related_name='superseded_by_jobs', blank=True, null=True,
                                            on_delete=models.PROTECT)
    superseded_job = models.OneToOneField('job.Job', related_name='superseded_by_job', blank=True, null=True,
                                          on_delete=models.PROTECT)
    delete_superseded = models.BooleanField(default=True)

    created = models.DateTimeField(auto_now_add=True)
    queued = models.DateTimeField(blank=True, null=True)
    started = models.DateTimeField(blank=True, null=True)
    ended = models.DateTimeField(blank=True, null=True)
    last_status_change = models.DateTimeField(blank=True, db_index=True, null=True)
    superseded = models.DateTimeField(blank=True, null=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = JobManager()

    def get_job_data(self):
        """Returns the data for this job

        :returns: The data for this job
        :rtype: :class:`job.configuration.data.job_data.JobData`
        """

        return JobData(self.data)

    def get_job_interface(self):
        """Returns the interface for this job

        :returns: The interface for this job
        :rtype: :class:`job.configuration.interface.job_interface.JobInterface`
        """

        return JobInterface(self.job_type_rev.interface)

    def get_job_results(self):
        """Returns the results for this job

        :returns: The results for this job
        :rtype: :class:`job.configuration.results.job_results.JobResults`
        """

        return JobResults(self.results)

    def get_resources(self):
        """Returns the resources required for this job

        :returns: The required resources
        :rtype: :class:`node.resources.node_resources.NodeResources`
        """

        resources = self.job_type.get_resources()

        # Calculate memory required in MiB rounded up to the nearest whole MiB
        multiplier = self.job_type.mem_mult_required
        const = self.job_type.mem_const_required
        disk_in_required = self.disk_in_required
        if not disk_in_required:
            disk_in_required = 0.0
        memory_mb = long(math.ceil(multiplier * disk_in_required + const))
        memory_required = max(memory_mb, MIN_MEM)

        # Calculate output space required in MiB rounded up to the nearest whole MiB
        multiplier = self.job_type.disk_out_mult_required
        const = self.job_type.disk_out_const_required
        output_size_mb = long(math.ceil(multiplier * disk_in_required + const))
        disk_out_required = max(output_size_mb, MIN_DISK)

        resources.add(NodeResources([Mem(memory_required), Disk(disk_out_required + disk_in_required)]))
        return resources

    def increase_max_tries(self):
        """Increase the total max_tries based on the current number of executions and job type max_tries.
        Callers must save the model to persist the change.
        """

        self.max_tries = self.num_exes + self.job_type.max_tries

    def _can_be_canceled(self):
        """Indicates whether this job can be canceled.

        :returns: True if the job status allows the job to be canceled, false otherwise.
        :rtype: bool
        """

        return self.status not in ['COMPLETED', 'CANCELED']
    can_be_canceled = property(_can_be_canceled)

    def _is_ready_to_queue(self):
        """Indicates whether this job can be added to the queue.

        :returns: True if the job status allows the job to be queued, false otherwise.
        :rtype: bool
        """

        return self.status in ['PENDING', 'CANCELED', 'FAILED']
    is_ready_to_queue = property(_is_ready_to_queue)

    def _is_ready_to_requeue(self):
        """Indicates whether this job can be added to the queue after being attempted previously.

        :returns: True if the job status allows the job to be queued, false otherwise.
        :rtype: bool
        """

        return self.status in ['CANCELED', 'FAILED']
    is_ready_to_requeue = property(_is_ready_to_requeue)

    class Meta(object):
        """meta information for the db"""
        db_table = 'job'
        index_together = ['last_modified', 'job_type', 'status']


class JobExecutionManager(models.Manager):
    """Provides additional methods for handling job executions."""

    def get_exes(self, started=None, ended=None, statuses=None, job_type_ids=None, job_type_names=None,
                 job_type_categories=None, node_ids=None, order=None):
        """Returns a list of jobs within the given time range.

        :param started: Query job executions updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query job executions updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param statuses: Query job executions with the a specific status.
        :type statuses: [string]
        :param job_type_ids: Query job executions of the type associated with the identifier.
        :type job_type_ids: [int]
        :param job_type_names: Query job executions of the type associated with the name.
        :type job_type_names: [string]
        :param job_type_categories: Query job executions of the type associated with the category.
        :type job_type_categories: [string]
        :param node_ids: Query job executions that ran on a node with the identifier.
        :type node_ids: [int]
        :param order: A list of fields to control the sort order.
        :type order: [string]
        :returns: The list of job executions that match the time range.
        :rtype: [:class:`job.models.JobExecution`]
        """

        # Fetch a list of job executions
        job_exes = JobExecution.objects.all().select_related('job', 'job__job_type', 'node', 'error')
        job_exes = job_exes.defer('stdout', 'stderr')

        # Apply time range filtering
        if started:
            job_exes = job_exes.filter(last_modified__gte=started)
        if ended:
            job_exes = job_exes.filter(last_modified__lte=ended)

        if statuses:
            job_exes = job_exes.filter(status__in=statuses)
        if job_type_ids:
            job_exes = job_exes.filter(job__job_type_id__in=job_type_ids)
        if job_type_names:
            job_exes = job_exes.filter(job__job_type__name__in=job_type_names)
        if job_type_categories:
            job_exes = job_exes.filter(job__job_type__category__in=job_type_categories)
        if node_ids:
            job_exes = job_exes.filter(node_id__in=node_ids)

        # Apply sorting
        if order:
            job_exes = job_exes.order_by(*order)
        else:
            job_exes = job_exes.order_by('last_modified')
        return job_exes

    def get_details(self, job_exe_id):
        """Gets additional details for the given job execution model based on related model attributes.

        :param job_exe_id: The unique identifier of the job execution.
        :type job_exe_id: int
        :returns: The job execution with extra related attributes.
        :rtype: :class:`job.models.JobExecution`
        """
        job_exe = JobExecution.objects.all().select_related(
            'job', 'job__job_type', 'job__error', 'job__event', 'job__event__rule', 'node', 'error'
        )
        job_exe = job_exe.defer('stdout', 'stderr')
        job_exe = job_exe.get(pk=job_exe_id)
        return job_exe

    def get_locked_job_exe(self, job_exe_id):
        """Returns the job execution with the given ID with a model lock obtained

        :param job_exe_id: The job execution ID
        :type job_exe_id: int
        :returns: The job execution model with a model lock
        :rtype: :class:`job.models.JobExecution`
        """

        return self.select_for_update().defer('stdout', 'stderr').get(pk=job_exe_id)

    def get_logs(self, job_exe_id):
        """Gets additional details for the given job execution model based on related model attributes.

        :param job_exe_id: The unique identifier of the job execution.
        :type job_exe_id: int
        :returns: The job execution with extra related attributes.
        :rtype: :class:`job.models.JobExecution`
        """
        job_exe = JobExecution.objects.all().select_related('job', 'job__job_type', 'node', 'error')
        job_exe = job_exe.get(pk=job_exe_id)

        return job_exe

    def get_job_exe_with_job_and_job_type(self, job_exe_id):
        """Gets a job execution with its related job and job_type models populated using only one database query

        :param job_exe_id: The ID of the job execution to retrieve
        :type job_exe_id: int
        :returns: The job execution model with related job and job_type models populated
        :rtype: :class:`job.models.JobExecution`
        """

        return self.select_related('job__job_type', 'job__job_type_rev').defer('stdout', 'stderr').get(pk=job_exe_id)

    def get_latest(self, jobs):
        """Gets the latest job execution associated with each given job.

        :param jobs: The jobs to populate with latest execution models.
        :type jobs: [class:`job.models.Job`]
        :returns: A dictionary that maps each job identifier to its latest execution.
        :rtype: dict of int -> class:`job.models.JobExecution`
        """
        job_exes = JobExecution.objects.filter(job__in=jobs).defer('stdout', 'stderr')

        results = {}
        for job_exe in job_exes:
            if job_exe.job_id not in results or job_exe.created > results[job_exe.job_id].created:
                results[job_exe.job_id] = job_exe

        return results

    def get_running_job_exes(self):
        """Returns all job executions that are currently RUNNING on a node

        :returns: The list of RUNNING job executions
        :rtype: list of :class:`job.models.JobExecution`
        """

        job_exe_qry = JobExecution.objects.defer('stdout', 'stderr')
        return job_exe_qry.filter(status='RUNNING')

    def post_steps_results(self, job_exe_id, results, results_manifest):
        """Updates the given job execution to reflect that the post-job steps have finished calculating the results

        :param job_exe_id: The job execution whose results have been processed
        :type job_exe_id: int
        :param results: The job execution results
        :type results: :class:`job.configuration.results.job_results.JobResults`
        :param results_manifest: The results manifest generated by the job execution
        :type results_manifest: :class:`job.configuration.results.results_manifest.results_manifest.ResultsManifest`
        """

        if not results or not results_manifest:
            raise Exception('Job execution results and results manifest are required')

        modified = timezone.now()
        self.filter(id=job_exe_id).update(results=results.get_dict(), results_manifest=results_manifest.get_json_dict(),
                                          last_modified=modified)

    def pre_steps_command_arguments(self, job_exe_id, command_arguments):
        """Updates the given job execution after the job command argument string has been filled out.

        This typically includes pre-job step information (e.g. location of file paths).

        :param job_exe_id: The job execution whose pre-job steps have filled out the job command
        :type job_exe_id: int
        :param command_arguments: The new job execution command argument string with pre-job step information filled in
        :type command_arguments: string
        """

        modified = timezone.now()
        self.filter(id=job_exe_id).update(command_arguments=command_arguments, last_modified=modified)

    def queue_job_exes(self, jobs, when):
        """Creates, saves, and returns new job executions for the given queued jobs. The caller must have obtained model
        locks on the job models. Any jobs that are not queued will be ignored. All jobs should have their related
        job_type and job_type_rev models populated.

        :param jobs: The queued jobs
        :type jobs: [:class:`job.models.Job`]
        :param when: The time that the jobs are queued
        :type when: :class:`datetime.datetime`
        :returns: The new queued job execution models
        :rtype: [:class:`job.models.JobExecution`]
        """

        job_ids = set()
        job_exes = []
        for job in jobs:
            if job.status != 'QUEUED':
                continue

            job_ids.add(job.id)
            job_exe = JobExecution()
            job_exe.job = job
            job_exe.timeout = job.timeout
            job_exe.queued = when
            job_exe.created = when
            # Fill in job execution command argument string with data that doesn't require pre-task
            interface = job.get_job_interface()
            data = job.get_job_data()
            job_exe.command_arguments = interface.populate_command_argument_properties(data)
            job_exe.configuration = job.configuration
            job_exes.append(job_exe)

        if not job_exes:
            return []

        # Create job executions and re-query to get ID fields
        self.bulk_create(job_exes)
        return list(self.filter(job_id__in=job_ids, status='QUEUED').iterator())

    def update_status(self, job_exes, status, when, error=None):
        """Updates the given job executions and jobs with the new status. The caller must have obtained model locks on
        the job execution and job models (in that order).

        :param job_exes: The job executions (with related job models) to update
        :type job_exes: [:class:`job.models.JobExecution`]
        :param status: The new status
        :type status: string
        :param when: The time that the status change occurred
        :type when: :class:`datetime.datetime`
        :param error: The error that caused the failure (required if status is FAILED, should be None otherwise)
        :type error: :class:`error.models.Error`
        """

        if status == 'QUEUED':
            raise Exception('QUEUED is an invalid status transition for job executions, use queue_job_exes()')
        if status == 'RUNNING':
            raise Exception('update_status() cannot set a job execution to RUNNING, use schedule_job_executions()')
        if status == 'FAILED' and not error:
            raise Exception('An error is required when status is FAILED')
        if not status == 'FAILED' and error:
            raise Exception('Status %s is invalid with an error' % status)

        modified = timezone.now()

        # Update job execution models in memory and collect job execution IDs
        job_exe_ids = set()
        jobs = []
        for job_exe in job_exes:
            job_exe_ids.add(job_exe.id)
            jobs.append(job_exe.job)
            job_exe.status = status
            job_exe.ended = when
            job_exe.error = error
            job_exe.last_modified = modified

        # Update job execution models in database with single query
        self.filter(id__in=job_exe_ids).update(status=status, ended=when, error=error, last_modified=modified)

        # Update job models
        Job.objects.update_status(jobs, status, when, error)


class JobExecution(models.Model):
    """Represents a job execution that has been scheduled to run on a node

    :keyword job: The job that was scheduled
    :type job: :class:`django.db.models.ForeignKey`
    :keyword job_type: The type of the job that was scheduled
    :type job_type: :class:`django.db.models.ForeignKey`
    :keyword exe_num: The number of the job's execution
    :type exe_num: :class:`django.db.models.IntegerField`
    :keyword cluster_id: This is an ID for the job execution that is unique in the context of the cluster, allowing
        Scale components (task IDs, Docker volume names, etc) to have unique names within the cluster
    :type cluster_id: :class:`django.db.models.CharField`
    :keyword node: The node on which the job execution is scheduled
    :type node: :class:`django.db.models.ForeignKey`

    :keyword timeout: The maximum amount of time to allow this execution to run before being killed (in seconds)
    :type timeout: :class:`django.db.models.IntegerField`
    :keyword input_file_size: The total amount of disk space in MiB for all input files for this job execution
    :type input_file_size: :class:`django.db.models.FloatField`
    :keyword resources: JSON description describing the resources allocated to this job execution
    :type resources: :class:`django.contrib.postgres.fields.JSONField`
    :keyword configuration: JSON description describing the configuration for how the job execution should be run
    :type configuration: :class:`django.contrib.postgres.fields.JSONField`

    :keyword queued: When the job execution was added to the queue
    :type queued: :class:`django.db.models.DateTimeField`
    :keyword started: When the job execution was started (scheduled)
    :type started: :class:`django.db.models.DateTimeField`
    :keyword created: When this model was created
    :type created: :class:`django.db.models.DateTimeField`
    """

    job = models.ForeignKey('job.Job', on_delete=models.PROTECT)
    job_type = models.ForeignKey('job.JobType', blank=True, null=True, on_delete=models.PROTECT)
    exe_num = models.IntegerField(blank=True, null=True)
    cluster_id = models.CharField(blank=True, max_length=100, null=True)
    node = models.ForeignKey('node.Node', blank=True, null=True, on_delete=models.PROTECT)

    timeout = models.IntegerField()
    input_file_size = models.FloatField(blank=True, null=True)
    resources = django.contrib.postgres.fields.JSONField(default=dict)
    configuration = django.contrib.postgres.fields.JSONField(default=dict)

    queued = models.DateTimeField()
    started = models.DateTimeField(blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)

    # TODO: old fields that are being nulled out, they should be removed in the future after they have been moved to the
    # new job_exe_end and job_exe_output tables and they are no longer needed for the REST API
    status = models.CharField(blank=True, max_length=50, null=True, db_index=True)
    error = models.ForeignKey('error.Error', blank=True, null=True, on_delete=models.PROTECT)
    command_arguments = models.CharField(blank=True, max_length=1000, null=True)
    environment = django.contrib.postgres.fields.JSONField(blank=True, null=True)
    cpus_scheduled = models.FloatField(blank=True, null=True)
    mem_scheduled = models.FloatField(blank=True, null=True)
    disk_out_scheduled = models.FloatField(blank=True, null=True)
    disk_total_scheduled = models.FloatField(blank=True, null=True)
    pre_started = models.DateTimeField(blank=True, null=True)
    pre_completed = models.DateTimeField(blank=True, null=True)
    pre_exit_code = models.IntegerField(blank=True, null=True)
    job_started = models.DateTimeField(blank=True, null=True)
    job_completed = models.DateTimeField(blank=True, null=True)
    job_exit_code = models.IntegerField(blank=True, null=True)
    job_metrics = django.contrib.postgres.fields.JSONField(blank=True, null=True)
    post_started = models.DateTimeField(blank=True, null=True)
    post_completed = models.DateTimeField(blank=True, null=True)
    post_exit_code = models.IntegerField(blank=True, null=True)
    stdout = models.TextField(blank=True, null=True)
    stderr = models.TextField(blank=True, null=True)
    results_manifest = django.contrib.postgres.fields.JSONField(blank=True, null=True)
    results = django.contrib.postgres.fields.JSONField(blank=True, null=True)
    ended = models.DateTimeField(blank=True, db_index=True, null=True)
    last_modified = models.DateTimeField(blank=True, db_index=True, null=True)

    objects = JobExecutionManager()

    def create_canceled_job_exe_end_model(self):
        """Creates and returns a canceled job execution end for this job execution

        :returns: The job execution end model
        :rtype: :class:`job.models.JobExecutionEnd`
        """

        job_exe_end = JobExecutionEnd()
        job_exe_end.job_exe_id = self.id
        job_exe_end.job_id = self.job_id
        job_exe_end.job_type = self.job_type_id
        job_exe_end.exe_num = self.exe_num
        job_exe_end.status = 'CANCELED'
        job_exe_end.queued = self.queued
        job_exe_end.started = self.started
        job_exe_end.ended = self.started
        return job_exe_end

    def get_cluster_id(self):
        """Gets the cluster ID for the job execution

        :returns: The cluster ID for the job execution
        :rtype: string
        """

        if not self.cluster_id:
            # Return old-style format before cluster_id field was created
            return 'scale_%d' % self.pk

        return self.cluster_id

    def get_execution_configuration(self):
        """Returns the execution configuration for this job

        :returns: The execution configuration for this job
        :rtype: :class:`job.configuration.json.execution.exe_config.ExecutionConfiguration`
        """

        return ExecutionConfiguration(self.configuration)

    def get_log_json(self, include_stdout=True, include_stderr=True, since=None):
        """Get log data from elasticsearch as a dict (from the raw JSON).

        :param include_stdout: If True, include stdout in the result
        :type include_stdout: bool
        :param include_stderr: If True include stderr in the result
        :type include_stderr: bool
        :param since: If present, only retrieve logs since this timestamp (non-inclusive).
        :type since: :class:`datetime.datetime` or None
        :rtype: tuple of (dict, :class:`datetime.datetime`) with the results or None and the last modified timestamp
        """

        if self.status == 'QUEUED':
            return None, timezone.now()

        q = {
                'size': 10000,
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'scale_job_exe.raw': self.get_cluster_id()}}
                        ]
                    }
                },
                'sort': [{'@timestamp': 'asc'}, {'scale_order_num': 'asc'}],
                '_source': ['@timestamp', 'scale_order_num', 'message', 'stream', 'scale_job_exe']
            }
        if not include_stdout and not include_stderr:
            return None, timezone.now()
        elif include_stdout and not include_stderr:
            q['query']['bool']['must'].append({'term': {'stream.raw': 'stdout'}})
        elif include_stderr and not include_stdout:
            q['query']['bool']['must'].append({'term': {'stream.raw': 'stderr'}})
        if since is not None:
            q['query']['bool']['must'].append({'range': {'@timestamp': {'gte': since.isoformat()}}})

        hits = settings.ELASTICSEARCH.search(index='_all', body=q)

        if hits['hits']['total'] == 0:
            return None, timezone.now()
        last_modified = max([util.parse.parse_datetime(h['_source']['@timestamp']) for h in hits['hits']['hits']])
        return hits, last_modified

    def get_log_text(self, include_stdout=True, include_stderr=True, since=None, html=False):
        """Get log data from elasticsearch.

        :param include_stdout: If True, include stdout in the result
        :type include_stdout: bool
        :param include_stderr: If True include stderr in the result
        :type include_stderr: bool
        :param since: If present, only retrieve logs since this timestamp (non-inclusive).
        :type since: :class:`datetime.datetime` or None
        :param html: If True, wrap the lines in div elements with stdout/stderr css classes, otherwise use plain text
        :type html: bool
        :rtype: tuple of (str, :class:`datetime.datetime`) with the log or None and last modified timestamp
        """

        hits, last_modified = self.get_log_json(include_stdout, include_stderr, since)
        if hits is None:
            return None, last_modified
        valid_hits = []  # Make sure hits have the required message field
        for h in hits['hits']['hits']:
            if 'message' in h['_source']:
                valid_hits.append(h)
        if html:
            d = ''
            for h in valid_hits:
                cls = h['_source']['stream']
                d += '<div class="%s">%s</div>\n' % (cls, django.utils.html.escape(h['_source']['message']))
            return d, last_modified
        return '\n'.join(h['_source']['message'] for h in valid_hits), last_modified

    def get_resources(self):
        """Returns the resources allocated to this job execution

        :returns: The allocated resources
        :rtype: :class:`node.resources.node_resources.NodeResources`
        """

        return Resources(self.resources).get_node_resources()

    @staticmethod
    def parse_cluster_id(task_id):
        """Parses and returns the cluster ID from the given task ID

        :param task_id: The task ID
        :type task_id: string
        :returns: The cluster ID
        :rtype: string
        """

        # Cluster ID is the first four segments
        segments = task_id.split('_')
        return '_'.join(segments[:4])

    def set_cluster_id(self, framework_id, job_id, exe_num):
        """Sets the unique cluster ID for this job execution

        :param framework_id: The scheduling framework ID
        :type framework_id: string
        :param job_id: The job ID
        :type job_id: int
        :param exe_num: The number of the execution
        :type exe_num: int
        """

        self.cluster_id = '%s_%s_%dx%d' % (JOB_TASK_ID_PREFIX, framework_id, job_id, exe_num)

    class Meta(object):
        """Meta information for the database"""
        db_table = 'job_exe'


class JobExecutionEnd(models.Model):
    """Represents the end of a job execution, including the execution's final status and end time

    :keyword job_exe: The primary key to the scheduled job execution
    :type job_exe: :class:`django.db.models.ForeignKey`
    :keyword job: The job that was executed
    :type job: :class:`django.db.models.ForeignKey`
    :keyword job_type: The type of the job that was executed
    :type job_type: :class:`django.db.models.ForeignKey`
    :keyword exe_num: The number of the job's execution
    :type exe_num: :class:`django.db.models.IntegerField`

    :keyword status: The final status of the execution
    :type status: :class:`django.db.models.CharField`
    :keyword error: The error that caused the failure (should only be set when status is FAILED)
    :type error: :class:`django.db.models.ForeignKey`
    :keyword node: The node on which the job execution was run (None if it was canceled before being scheduled)
    :type node: :class:`django.db.models.ForeignKey`

    :keyword queued: When the job execution was added to the queue
    :type queued: :class:`django.db.models.DateTimeField`
    :keyword started: When the job execution was started (scheduled)
    :type started: :class:`django.db.models.DateTimeField`
    :keyword ended: When the job execution ended
    :type ended: :class:`django.db.models.DateTimeField`
    :keyword created: When this model was created
    :type created: :class:`django.db.models.DateTimeField`
    """

    JOB_EXE_STATUSES = (
        ('FAILED', 'FAILED'),
        ('COMPLETED', 'COMPLETED'),
        ('CANCELED', 'CANCELED'),
    )

    job_exe = models.OneToOneField('job.JobExecution', primary_key=True, on_delete=models.PROTECT)
    job = models.ForeignKey('job.Job', on_delete=models.PROTECT)
    job_type = models.ForeignKey('job.JobType', on_delete=models.PROTECT)
    exe_num = models.IntegerField()

    status = models.CharField(choices=JOB_EXE_STATUSES, max_length=50, db_index=True)
    error = models.ForeignKey('error.Error', blank=True, null=True, on_delete=models.PROTECT)
    node = models.ForeignKey('node.Node', blank=True, null=True, on_delete=models.PROTECT)

    queued = models.DateTimeField()
    started = models.DateTimeField(blank=True, db_index=True, null=True)
    ended = models.DateTimeField(db_index=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(object):
        """Meta information for the database"""
        db_table = 'job_exe_end'


class JobExecutionOutput(models.Model):
    """Represents the output of a job execution

    :keyword job_exe: The primary key to the scheduled job execution
    :type job_exe: :class:`django.db.models.ForeignKey`
    :keyword job: The job that was executed
    :type job: :class:`django.db.models.ForeignKey`
    :keyword job_type: The type of the job that was executed
    :type job_type: :class:`django.db.models.ForeignKey`
    :keyword exe_num: The number of the job's execution
    :type exe_num: :class:`django.db.models.IntegerField`

    :keyword output_manifest: The output manifest generated by the job execution
    :type output_manifest: :class:`django.contrib.postgres.fields.JSONField`
    :keyword output: JSON description of the job execution's output
    :type output: :class:`django.contrib.postgres.fields.JSONField`

    :keyword created: When this model was created
    :type created: :class:`django.db.models.DateTimeField`
    """

    job_exe = models.OneToOneField('job.JobExecution', primary_key=True, on_delete=models.PROTECT)
    job = models.ForeignKey('job.Job', on_delete=models.PROTECT)
    job_type = models.ForeignKey('job.JobType', on_delete=models.PROTECT)
    exe_num = models.IntegerField()

    output_manifest = django.contrib.postgres.fields.JSONField(blank=True, null=True)
    output = django.contrib.postgres.fields.JSONField(default=dict)

    created = models.DateTimeField(auto_now_add=True)

    def get_output(self):
        """Returns the output for this job execution

        :returns: The output for this job execution
        :rtype: :class:`job.configuration.results.job_results.JobResults`
        """

        return JobResults(self.output)

    class Meta(object):
        """Meta information for the database"""
        db_table = 'job_exe_output'


class JobInputFile(models.Model):
    """Links a job and its input files together. A file can be used as input to multiple jobs and a job can
    accept multiple input files.

    :keyword job: The job that the input file is linked to
    :type job: :class:`django.db.models.ForeignKey`
    :keyword input_file: The input file that is linked to the job
    :type input_file: :class:`django.db.models.ForeignKey`
    :keyword job_input: The name of the job input that the file was passed into
    :type job_input: :class:`django.db.models.CharField`
    :keyword created: When this link was created
    :type created: :class:`django.db.models.DateTimeField`
    """

    job = models.ForeignKey('job.Job', on_delete=models.PROTECT)
    input_file = models.ForeignKey('storage.ScaleFile', on_delete=models.PROTECT)
    job_input = models.CharField(max_length=250)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(object):
        """meta information for the db"""
        db_table = 'job_input_file'


class JobTypeStatusCounts(object):
    """Represents job counts for a job type.

    :keyword status: The job execution status being counted.
    :type status: string
    :keyword count: The number of job executions for the associated status.
    :type count: int
    :keyword most_recent: The date/time of the last job execution for the associated status.
    :type most_recent: datetime.datetime
    :keyword category: The category of the job execution status being counted. Note that currently this will only be
        populated for types of ERROR status values.
    :type category: string
    """
    def __init__(self, status, count=0, most_recent=None, category=None):
        self.status = status
        self.count = count
        self.most_recent = most_recent
        self.category = category


class JobTypeStatus(object):
    """Represents job type statistics.

    :keyword job_type: The job type being counted.
    :type job_type: :class:`job.models.JobType`
    :keyword job_counts: A list of counts for the jobs of the given job type organized by status.
    :type job_counts: [:class:`job.models.JobTypeStatusCounts`]
    """
    def __init__(self, job_type, job_counts=None):
        self.job_type = job_type
        self.job_counts = job_counts


class JobTypePendingStatus(object):
    """Represents job type pending statistics.

    :keyword job_type: The job type being counted.
    :type job_type: :class:`job.models.JobType`
    :keyword count: The number of job executions pending for the associated job type.
    :type count: int
    :keyword longest_pending: The date/time of the last job execution for the associated job type.
    :type longest_pending: datetime.datetime
    """
    def __init__(self, job_type, count=0, longest_pending=None):
        self.job_type = job_type
        self.count = count
        self.longest_pending = longest_pending


class JobTypeRunningStatus(object):
    """Represents job type running statistics.

    :keyword job_type: The job type being counted.
    :type job_type: :class:`job.models.JobType`
    :keyword count: The number of job executions running for the associated job type.
    :type count: int
    :keyword longest_running: The date/time of the last job execution for the associated job type.
    :type longest_running: datetime.datetime
    """
    def __init__(self, job_type, count=0, longest_running=None):
        self.job_type = job_type
        self.count = count
        self.longest_running = longest_running


class JobTypeFailedStatus(object):
    """Represents job type system failure statistics.

    :keyword job_type: The job type being counted.
    :type job_type: :class:`job.models.JobType`
    :keyword count: The number of job executions failed for the associated job type.
    :type count: int
    :keyword first_error: The date/time of the first job execution failed for the associated job type.
    :type first_error: datetime.datetime
    :keyword last_error: The date/time of the last job execution failed for the associated job type.
    :type last_error: datetime.datetime
    """
    def __init__(self, job_type, error, count=0, first_error=None, last_error=None):
        self.job_type = job_type
        self.error = error
        self.count = count
        self.first_error = first_error
        self.last_error = last_error


class JobTypeManager(models.Manager):
    """Provides additional methods for handling job types
    """

    @transaction.atomic
    def create_job_type(self, name, version, interface, trigger_rule=None, error_mapping=None, custom_resources=None,
                        configuration=None, secrets=None, **kwargs):
        """Creates a new non-system job type and saves it in the database. All database changes occur in an atomic
        transaction.

        :param name: The identifying name of the job type used by clients for queries
        :type name: string
        :param version: The version of the job type
        :type version: string
        :param interface: The interface for running a job of this type
        :type interface: :class:`job.configuration.interface.job_interface.JobInterface`
        :param trigger_rule: The trigger rule that creates jobs of this type, possibly None
        :type trigger_rule: :class:`trigger.models.TriggerRule`
        :param error_mapping: Mapping for translating an exit code to an error type
        :type error_mapping: :class:`job.configuration.interface.error_interface.ErrorInterface`
        :param custom_resources: Custom resources required by this job type
        :type custom_resources: :class:`node.resources.json.resources.Resources`
        :param configuration: The configuration for running a job of this type, possibly None
        :type configuration: :class:`job.configuration.json.job.job_config.JobConfiguration`
        :param secrets: Secret settings required by this job type
        :type secrets: dict
        :returns: The new job type
        :rtype: :class:`job.models.JobType`

        :raises :class:`job.exceptions.InvalidJobField`: If a given job type field has an invalid value
        :raises :class:`trigger.configuration.exceptions.InvalidTriggerType`: If the given trigger rule is an invalid
        type for creating jobs
        :raises :class:`trigger.configuration.exceptions.InvalidTriggerRule`: If the given trigger rule configuration is
        invalid
        :raises :class:`job.configuration.data.exceptions.InvalidConnection`: If the trigger rule connection to the job
        type interface is invalid
        """

        for field_name in kwargs:
            if field_name in JobType.UNEDITABLE_FIELDS:
                raise Exception('%s is not an editable field' % field_name)
        self._validate_job_type_fields(**kwargs)

        # Validate the trigger rule
        if trigger_rule:
            trigger_config = trigger_rule.get_configuration()
            if not isinstance(trigger_config, JobTriggerRuleConfiguration):
                raise InvalidTriggerType('%s is an invalid trigger rule type for creating jobs' % trigger_rule.type)
            trigger_config.validate_trigger_for_job(interface)

        # Create the new job type
        job_type = JobType(**kwargs)
        job_type.name = name
        job_type.version = version
        job_type.interface = interface.get_dict()
        job_type.trigger_rule = trigger_rule
        if configuration:
            configuration.validate(job_type.interface)
            job_type.configuration = configuration.get_dict()
        if error_mapping:
            error_mapping.validate()
            job_type.error_mapping = error_mapping.get_dict()
        if custom_resources:
            job_type.custom_resources = custom_resources.get_dict()
        if 'is_active' in kwargs:
            job_type.archived = None if kwargs['is_active'] else timezone.now()
        if 'is_paused' in kwargs:
            job_type.paused = timezone.now() if kwargs['is_paused'] else None
        job_type.save()

        # Save any secrets to Vault
        if secrets:
            self.set_job_type_secrets(job_type.get_secrets_key(), secrets)

        # Create first revision of the job type
        JobTypeRevision.objects.create_job_type_revision(job_type)

        return job_type

    @transaction.atomic
    def edit_job_type(self, job_type_id, interface=None, trigger_rule=None, remove_trigger_rule=False,
                      error_mapping=None, custom_resources=None, configuration=None, secrets=None, **kwargs):
        """Edits the given job type and saves the changes in the database. The caller must provide the related
        trigger_rule model. All database changes occur in an atomic transaction. An argument of None for a field
        indicates that the field should not change. The remove_trigger_rule parameter indicates the difference between
        no change to the trigger rule (False) and removing the trigger rule (True) when trigger_rule is None.

        :param job_type_id: The unique identifier of the job type to edit
        :type job_type_id: int
        :param interface: The interface for running a job of this type, possibly None
        :type interface: :class:`job.configuration.interface.job_interface.JobInterface`
        :param trigger_rule: The trigger rule that creates jobs of this type, possibly None
        :type trigger_rule: :class:`trigger.models.TriggerRule`
        :param remove_trigger_rule: Indicates whether the trigger rule should be unchanged (False) or removed (True)
            when trigger_rule is None
        :type remove_trigger_rule: bool
        :param error_mapping: Mapping for translating an exit code to an error type
        :type error_mapping: :class:`job.configuration.interface.error_interface.ErrorInterface`
        :param custom_resources: Custom resources required by this job type
        :type custom_resources: :class:`node.resources.json.resources.Resources`
        :param configuration: The configuration for running a job of this type, possibly None
        :type configuration: :class:`job.configuration.json.job.job_config.JobConfiguration`
        :param secrets: Secret settings required by this job type
        :type secrets: dict

        :raises :class:`job.exceptions.InvalidJobField`: If a given job type field has an invalid value
        :raises :class:`trigger.configuration.exceptions.InvalidTriggerType`: If the given trigger rule is an invalid
        type for creating jobs
        :raises :class:`trigger.configuration.exceptions.InvalidTriggerRule`: If the given trigger rule configuration is
        invalid
        :raises :class:`job.configuration.data.exceptions.InvalidConnection`: If the trigger rule connection to the job
        type interface is invalid
        :raises :class:`recipe.configuration.definition.exceptions.InvalidDefinition`: If the interface change
        invalidates any existing recipe type definitions
        """

        for field_name in kwargs:
            if field_name in JobType.UNEDITABLE_FIELDS:
                raise Exception('%s is not an editable field' % field_name)
        self._validate_job_type_fields(**kwargs)

        recipe_types = []
        if interface:
            # Lock all recipe types so they can be validated after changing job type interface
            from recipe.models import RecipeType
            recipe_types = list(RecipeType.objects.select_for_update().order_by('id').iterator())

        # Acquire model lock for job type
        job_type = JobType.objects.select_for_update().get(pk=job_type_id)
        if job_type.is_system:
            if len(kwargs) > 1 or 'is_paused' not in kwargs:
                raise InvalidJobField('You can only modify the is_paused field for a System Job')

        if interface:
            # New job interface, validate all existing recipes
            job_type.interface = interface.get_dict()
            job_type.revision_num += 1
            job_type.save()
            for recipe_type in recipe_types:
                recipe_type.get_recipe_definition().validate_job_interfaces()

        # New job configuration
        if configuration:
            configuration.validate(job_type.interface)
            job_type.configuration = configuration.get_dict()

        if trigger_rule or remove_trigger_rule:
            if job_type.trigger_rule:
                # Archive old trigger rule since we are changing to a new one
                TriggerRule.objects.archive_trigger_rule(job_type.trigger_rule_id)
            job_type.trigger_rule = trigger_rule

        # Validate updated trigger rule against updated interface
        if job_type.trigger_rule:
            trigger_config = job_type.trigger_rule.get_configuration()
            if not isinstance(trigger_config, JobTriggerRuleConfiguration):
                msg = '%s is an invalid trigger rule type for creating jobs'
                raise InvalidTriggerType(msg % job_type.trigger_rule.type)
            trigger_config.validate_trigger_for_job(job_type.get_job_interface())

        if error_mapping:
            error_mapping.validate()
            job_type.error_mapping = error_mapping.get_dict()

        if custom_resources:
            job_type.custom_resources = custom_resources.get_dict()

        if 'is_active' in kwargs and job_type.is_active != kwargs['is_active']:
            job_type.archived = None if kwargs['is_active'] else timezone.now()
        if 'is_paused' in kwargs and job_type.is_paused != kwargs['is_paused']:
            job_type.paused = timezone.now() if kwargs['is_paused'] else None
        for field_name in kwargs:
            setattr(job_type, field_name, kwargs[field_name])
        job_type.save()

        # Save any secrets to Vault
        if secrets:
            self.set_job_type_secrets(job_type.get_secrets_key(), secrets)

        if interface:
            # Create new revision of the job type for new interface
            JobTypeRevision.objects.create_job_type_revision(job_type)

    def get_by_natural_key(self, name, version):
        """Django method to retrieve a job type for the given natural key

        :param name: The human-readable name of the job type
        :type name: string
        :param version: The version of the job type
        :type version: string
        :returns: The job type defined by the natural key
        :rtype: :class:`job.models.JobType`
        """
        return self.get(name=name, version=version)

    def get_clock_job_type(self):
        """Returns the Scale Clock job type

        :returns: The clock job type
        :rtype: :class:`job.models.JobType`
        """

        return JobType.objects.get(name='scale-clock', version='1.0')

    def get_job_types(self, started=None, ended=None, names=None, categories=None, is_active=True, is_operational=None,
                      order=None):
        """Returns a list of job types within the given time range.

        :param started: Query job types updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query job types updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param names: Query jobs of the type associated with the name.
        :type names: [string]
        :param categories: Query jobs of the type associated with the category.
        :type categories: [string]
        :param is_active: Query job types that are actively available for use.
        :type is_active: bool
        :param is_operational: Query job types that are operational or research phase.
        :type is_operational: bool
        :param order: A list of fields to control the sort order.
        :type order: [string]
        :returns: The list of job types that match the time range.
        :rtype: [:class:`job.models.JobType`]
        """

        # Fetch a list of job types
        job_types = JobType.objects.all()

        # Apply time range filtering
        if started:
            job_types = job_types.filter(last_modified__gte=started)
        if ended:
            job_types = job_types.filter(last_modified__lte=ended)

        # Apply additional filters
        if names:
            job_types = job_types.filter(name__in=names)
        if categories:
            job_types = job_types.filter(category__in=categories)
        if is_active is not None:
            job_types = job_types.filter(is_active=is_active)
        if is_operational is not None:
            job_types = job_types.filter(is_operational=is_operational)

        # Apply sorting
        if order:
            job_types = job_types.order_by(*order)
        else:
            job_types = job_types.order_by('last_modified')
        return job_types

    def get_details(self, job_type_id):
        """Returns the job type for the given ID with all detail fields included.

        The additional fields include: errors, job_counts_6h, job_counts_12h, and job_counts_24h.

        :param job_type_id: The unique identifier of the job type.
        :type job_type_id: int
        :returns: The job type with all detail fields included.
        :rtype: :class:`job.models.JobType`
        """

        # Attempt to get the job type
        job_type = JobType.objects.select_related('trigger_rule').get(pk=job_type_id)

        # Add associated error information
        error_names = job_type.get_error_interface().get_error_names()
        job_type.errors = Error.objects.filter(name__in=error_names) if error_names else []

        # Scrub configuration for secrets
        if job_type.configuration:
            configuration = JobConfiguration(job_type.configuration)
            configuration.validate(job_type.interface)
            job_type.configuration = configuration.get_dict()

        # Add recent performance statistics
        started = timezone.now()
        job_type.job_counts_24h = self.get_performance(job_type_id, started - datetime.timedelta(hours=24))
        job_type.job_counts_12h = self.get_performance(job_type_id, started - datetime.timedelta(hours=12))
        job_type.job_counts_6h = self.get_performance(job_type_id, started - datetime.timedelta(hours=6))

        return job_type

    def get_performance(self, job_type_id, started, ended=None):
        """Returns the job count statistics for a given job type and time range.

        :param job_type_id: The unique identifier of the job type.
        :type job_type_id: int
        :param started: Query job types updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query job types updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :returns: A list of job counts organized by status.
        :rtype: [:class:`job.models.JobTypeStatusCounts`]
        """
        count_dicts = Job.objects.values('job_type__id', 'status', 'error__category')
        count_dicts = count_dicts.filter(job_type_id=job_type_id, last_status_change__gte=started)
        if ended:
            count_dicts = count_dicts.filter(last_status_change__lte=ended)
        count_dicts = count_dicts.annotate(count=models.Count('job_type'),
                                           most_recent=models.Max('last_status_change'))
        results = []
        for count_dict in count_dicts:
            counts = JobTypeStatusCounts(count_dict['status'], count_dict['count'],
                                         count_dict['most_recent'], count_dict['error__category'])
            results.append(counts)
        return results

    def get_status(self, started, ended=None, is_operational=None):
        """Returns a list of job types with counts broken down by job status.

        Note that all running job types are counted regardless of date/time filters.

        :param started: Query job types updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query job types updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param is_operational: Query job types that are operational or research phase.
        :type is_operational: bool
        :returns: The list of job types with supplemented statistics.
        :rtype: [:class:`job.models.JobTypeStatus`]
        """

        # Build a mapping of all job type identifier -> status model
        job_types = JobType.objects.all().defer('interface', 'error_mapping').order_by('last_modified')
        if is_operational is not None:
            job_types = job_types.filter(is_operational=is_operational)
        status_dict = {job_type.id: JobTypeStatus(job_type, []) for job_type in job_types}

        # Build up the filters based on inputs and all running jobs
        count_filters = Q(status='RUNNING')
        if ended:
            count_filters = count_filters | Q(last_status_change__gte=started, last_status_change__lte=ended)
        else:
            count_filters = count_filters | Q(last_status_change__gte=started)

        # Fetch a count of all jobs grouped by status counts
        count_dicts = Job.objects.values('job_type__id', 'status', 'error__category').filter(count_filters)
        if is_operational is not None:
            count_dicts = count_dicts.filter(job_type__is_operational=is_operational)
        count_dicts = count_dicts.annotate(count=models.Count('job_type'),
                                           most_recent=models.Max('last_status_change'))

        # Collect the status and counts by job type
        for count_dict in count_dicts:
            status = status_dict[count_dict['job_type__id']]
            counts = JobTypeStatusCounts(count_dict['status'], count_dict['count'],
                                         count_dict['most_recent'], count_dict['error__category'])
            status.job_counts.append(counts)

        return [status_dict[job_type.id] for job_type in job_types]

    def get_pending_status(self):
        """Returns a status overview of all currently pending job types.

        The results consist of standard job type models, plus additional computed statistics fields including a total
        count of associated jobs and the longest pending job.

        :returns: The list of each job type with additional statistic fields.
        :rtype: [:class:`job.models.JobTypePendingStatus`]
        """

        # Fetch a count of all pending jobs with type information
        # We have to specify values to workaround the JSON fields throwing an error when used with annotate
        job_dicts = Job.objects.values(*['job_type__%s' % f for f in JobType.BASE_FIELDS])
        job_dicts = job_dicts.filter(status='PENDING')
        job_dicts = job_dicts.annotate(count=models.Count('job_type'),
                                       longest_pending=models.Min('last_status_change'))
        job_dicts = job_dicts.order_by('longest_pending')

        # Convert each result to a real job type model with added statistics
        results = []
        for job_dict in job_dicts:
            job_type_dict = {f: job_dict['job_type__%s' % f] for f in JobType.BASE_FIELDS}
            job_type = JobType(**job_type_dict)

            status = JobTypePendingStatus(job_type, job_dict['count'], job_dict['longest_pending'])
            results.append(status)
        return results

    def get_running_status(self):
        """Returns a status overview of all currently running job types.

        The results consist of standard job type models, plus additional computed statistics fields including a total
        count of associated jobs and the longest running job.

        :returns: The list of each job type with additional statistic fields.
        :rtype: [:class:`job.models.JobTypeRunningStatus`]
        """

        # Fetch a count of all running jobs with type information
        # We have to specify values to workaround the JSON fields throwing an error when used with annotate
        job_dicts = Job.objects.values(*['job_type__%s' % f for f in JobType.BASE_FIELDS])
        job_dicts = job_dicts.filter(status='RUNNING')
        job_dicts = job_dicts.annotate(count=models.Count('job_type'),
                                       longest_running=models.Min('last_status_change'))
        job_dicts = job_dicts.order_by('longest_running')

        # Convert each result to a real job type model with added statistics
        results = []
        for job_dict in job_dicts:
            job_type_dict = {f: job_dict['job_type__%s' % f] for f in JobType.BASE_FIELDS}
            job_type = JobType(**job_type_dict)

            status = JobTypeRunningStatus(job_type, job_dict['count'], job_dict['longest_running'])
            results.append(status)
        return results

    def get_failed_status(self):
        """Returns all job types that have failed due to system errors.

        The results consist of standard job type models, plus additional computed statistics fields including a total
        count of associated jobs and the last status change of a running job.

        :returns: The list of each job type with additional statistic fields.
        :rtype: [:class:`job.models.JobTypeFailedStatus`]
        """

        # Make a list of all the basic error fields to fetch
        error_fields = ['id', 'name', 'title', 'description', 'category', 'created', 'last_modified']

        # We have to specify values to workaround the JSON fields throwing an error when used with annotate
        query_fields = []
        query_fields.extend(['job_type__%s' % f for f in JobType.BASE_FIELDS])
        query_fields.extend(['error__%s' % f for f in error_fields])

        # Fetch a count of all running jobs with type information
        job_dicts = Job.objects.values(*query_fields)
        job_dicts = job_dicts.filter(status='FAILED', error__category='SYSTEM')
        job_dicts = job_dicts.annotate(count=models.Count('job_type'),
                                       first_error=models.Min('last_status_change'),
                                       last_error=models.Max('last_status_change'))
        job_dicts = job_dicts.order_by('-last_error')

        # Convert each result to a real job type model with added statistics
        results = []
        for job_dict in job_dicts:
            job_type_dict = {f: job_dict['job_type__%s' % f] for f in JobType.BASE_FIELDS}
            job_type = JobType(**job_type_dict)

            error_dict = {f: job_dict['error__%s' % f] for f in error_fields}
            error = Error(**error_dict)

            status = JobTypeFailedStatus(job_type, error, job_dict['count'], job_dict['first_error'],
                                         job_dict['last_error'])
            results.append(status)
        return results

    def set_job_type_secrets(self, secrets_key, secrets):
        """Sends request to SecretsHandler to write secrets for a job type.

        :param secrets_key: Reference pointer for job_type settings stored in secrets backend
        :type secrets_key: str
        :param secrets: Secret settings required by this job type.
        :type secrets: dict
        """

        secrets_handler = SecretsHandler()
        secrets_handler.set_job_type_secrets(secrets_key, secrets)

    def validate_job_type(self, name, version, interface, error_mapping=None, trigger_config=None, configuration=None):
        """Validates a new job type prior to attempting a save

        :param name: The system name of the job type
        :type name: string
        :param version: The version of the job type
        :type version: string
        :param interface: The interface for running a job of this type
        :type interface: :class:`job.configuration.interface.job_interface.JobInterface`
        :param error_mapping: The interface for mapping error exit codes
        :type error_mapping: :class:`job.configuration.interface.error_interface.ErrorInterface`
        :param trigger_config: The trigger rule configuration, possibly None
        :type trigger_config: :class:`trigger.configuration.trigger_rule.TriggerRuleConfiguration`
        :param configuration: The configuration for running a job of this type, possibly None
        :type configuration: :class:`job.configuration.json.job.job_config.JobConfiguration`
        :returns: A list of warnings discovered during validation.
        :rtype: [:class:`job.configuration.data.job_data.ValidationWarning`]

        :raises :class:`trigger.configuration.exceptions.InvalidTriggerType`: If the given trigger rule is an invalid
            type for creating jobs
        :raises :class:`trigger.configuration.exceptions.InvalidTriggerRule`: If the given trigger rule configuration
            is invalid
        :raises :class:`job.configuration.data.exceptions.InvalidConnection`: If the trigger rule connection to the job
            type interface is invalid
        :raises :class:`recipe.configuration.definition.exceptions.InvalidDefinition`: If the interface invalidates any
            existing recipe type definitions
        """

        warnings = []

        if trigger_config:
            trigger_config.validate()
            if not isinstance(trigger_config, JobTriggerRuleConfiguration):
                msg = '%s is an invalid trigger rule type for creating jobs'
                raise InvalidTriggerType(msg % trigger_config.trigger_rule_type)
            warnings.extend(trigger_config.validate_trigger_for_job(interface))

        if configuration:
            warnings.extend(configuration.validate(interface.get_dict()))

        if error_mapping:
            warnings.extend(error_mapping.validate())

        try:
            # If this is an existing job type, try changing the interface temporarily and validate all existing recipe
            # type definitions
            with transaction.atomic():
                job_type = JobType.objects.get(name=name, version=version)
                job_type.interface = interface.get_dict()
                job_type.save()

                from recipe.models import RecipeType
                for recipe_type in RecipeType.objects.all():
                    warnings.extend(recipe_type.get_recipe_definition().validate_job_interfaces())

                # Explicitly roll back transaction so job type isn't changed
                raise RollbackTransaction()
        except (JobType.DoesNotExist, RollbackTransaction):
            # Swallow exceptions
            pass

        return warnings

    def _validate_job_type_fields(self, **kwargs):
        """Validates the given keyword argument fields for job types

        :raises :class:`job.exceptions.InvalidJobField`: If a given job type field has an invalid value
        """

        if 'timeout' in kwargs:
            timeout = kwargs['timeout']
            if not timeout > 0:
                raise InvalidJobField('timeout must be greater than zero')
        if 'max_tries' in kwargs:
            max_tries = kwargs['max_tries']
            if not max_tries > 0:
                raise InvalidJobField('max_tries must be greater than zero')


class JobType(models.Model):
    """Represents a type of job that can be run on the cluster. Any updates to a job type model requires obtaining a
    lock on the model using select_for_update().

    :keyword name: The identifying name of the job type used by clients for queries
    :type name: :class:`django.db.models.CharField`
    :keyword version: The version of the job type
    :type version: :class:`django.db.models.CharField`
    :keyword title: The human-readable name of the job type
    :type title: :class:`django.db.models.CharField`
    :keyword description: An optional description of the job type
    :type description: :class:`django.db.models.TextField`
    :keyword category: An optional overall category of the job type
    :type category: :class:`django.db.models.CharField`
    :keyword author_name: The name of the person or organization that created the associated algorithm
    :type author_name: :class:`django.db.models.CharField`
    :keyword author_url: The address to a home page about the author or associated algorithm
    :type author_url: :class:`django.db.models.TextField`

    :keyword is_system: Whether this is a system type
    :type is_system: :class:`django.db.models.BooleanField`
    :keyword is_long_running: Whether this type is long running. A job of this type is intended to run for a long time,
        potentially indefinitely, without timing out and always being re-queued after a failure
    :type is_long_running: :class:`django.db.models.BooleanField`
    :keyword is_active: Whether the job type is active (false once job type is archived)
    :type is_active: :class:`django.db.models.BooleanField`
    :keyword is_operational: Whether this job type is operational (True) or is still in a research & development (R&D)
        phase (False)
    :type is_operational: :class:`django.db.models.BooleanField`
    :keyword is_paused: Whether the job type is paused (while paused no jobs of this type will be scheduled off of the
        queue)
    :type is_paused: :class:`django.db.models.BooleanField`

    :keyword uses_docker: Whether the job type uses Docker
    :type uses_docker: :class:`django.db.models.BooleanField`
    :keyword docker_privileged: Whether the job type uses Docker in privileged mode
    :type docker_privileged: :class:`django.db.models.BooleanField`
    :keyword docker_image: The Docker image containing the code to run for this job (if uses_docker is True)
    :type docker_image: :class:`django.db.models.CharField`
    :keyword interface: JSON description defining the interface for running a job of this type
    :type interface: :class:`django.contrib.postgres.fields.JSONField`
    :keyword docker_params: JSON array of 2-tuples (key-value) which will be passed as-is to docker.
        See the mesos prototype file for further information.
    :type docker_params: :class:`django.contrib.postgres.fields.JSONField`
    :keyword revision_num: The current revision number of the interface, starts at one
    :type revision_num: :class:`django.db.models.IntegerField`
    :keyword error_mapping: Mapping for translating an exit code to an error type
    :type error_mapping: :class:`django.contrib.postgres.fields.JSONField`
    :keyword trigger_rule: The rule to trigger new jobs of this type
    :type trigger_rule: :class:`django.db.models.ForeignKey`
    :keyword configuration: JSON describing the default job configuration for jobs of this type
    :type configuration: :class:`django.contrib.postgres.fields.JSONField`

    :keyword priority: The priority of the job type (lower number is higher priority)
    :type priority: :class:`django.db.models.IntegerField`
    :keyword max_scheduled: The maximum number of jobs of this type that may be scheduled to run at the same time
    :type max_scheduled: :class:`django.db.models.IntegerField`
    :keyword timeout: The maximum amount of time to allow a job of this type to run before being killed (in seconds)
    :type timeout: :class:`django.db.models.IntegerField`
    :keyword max_tries: The maximum number of times to try executing a job in case of errors (minimum one)
    :type max_tries: :class:`django.db.models.IntegerField`
    :keyword cpus_required: The number of CPUs required for a job of this type
    :type cpus_required: :class:`django.db.models.FloatField`
    :keyword mem_const_required: A constant amount of RAM in MiB required for a job of this type
    :type mem_const_required: :class:`django.db.models.FloatField`
    :keyword mem_mult_required: A multiplier (2x = 2.0) applied to the size of the input files to determine additional
        RAM in MiB required for a job of this type
    :type mem_mult_required: :class:`django.db.models.FloatField`
    :keyword shared_mem_required: The amount of shared memory (/dev/shm) in MiB required for a job of this type
    :type shared_mem_required: :class:`django.db.models.FloatField`
    :keyword disk_out_const_required: A constant amount of disk space in MiB required for job output (temp work and
        products) for a job of this type
    :type disk_out_const_required: :class:`django.db.models.FloatField`
    :keyword disk_out_mult_required: A multiplier (2x = 2.0) applied to the size of the input files to determine
        additional disk space in MiB required for job output (temp work and products) for a job of this type
    :type disk_out_mult_required: :class:`django.db.models.FloatField`
    :keyword custom_resources: JSON describing the custom resources required for jobs of this type
    :type custom_resources: :class:`django.contrib.postgres.fields.JSONField`

    :keyword icon_code: A font-awesome icon code (like 'f013' for gear) to use when representing this job type
    :type icon_code: string of a FontAwesome icon code

    :keyword created: When the job type was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword archived: When the job type was archived (no longer active)
    :type archived: :class:`django.db.models.DateTimeField`
    :keyword paused: When the job type was paused
    :type paused: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the job type was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    """

    BASE_FIELDS = ('id', 'name', 'version', 'title', 'description', 'category', 'author_name', 'author_url',
                   'is_system', 'is_long_running', 'is_active', 'is_operational', 'is_paused', 'icon_code')

    UNEDITABLE_FIELDS = ('name', 'version', 'is_system', 'is_long_running', 'is_active', 'uses_docker', 'revision_num',
                         'created', 'archived', 'paused', 'last_modified')

    name = models.CharField(db_index=True, max_length=50)
    version = models.CharField(db_index=True, max_length=50)
    title = models.CharField(blank=True, max_length=50, null=True)
    description = models.TextField(blank=True, null=True)
    category = models.CharField(db_index=True, blank=True, max_length=50, null=True)
    author_name = models.CharField(blank=True, max_length=50, null=True)
    author_url = models.TextField(blank=True, null=True)

    is_system = models.BooleanField(default=False)
    is_long_running = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_operational = models.BooleanField(default=True)
    is_paused = models.BooleanField(default=False)

    uses_docker = models.BooleanField(default=True)
    docker_privileged = models.BooleanField(default=False)
    docker_image = models.CharField(blank=True, null=True, max_length=500)
    interface = django.contrib.postgres.fields.JSONField(default=dict)
    docker_params = django.contrib.postgres.fields.JSONField(default=dict)
    revision_num = models.IntegerField(default=1)
    error_mapping = django.contrib.postgres.fields.JSONField(default=dict)
    trigger_rule = models.ForeignKey('trigger.TriggerRule', blank=True, null=True, on_delete=models.PROTECT)

    configuration = django.contrib.postgres.fields.JSONField(default=dict)

    priority = models.IntegerField(default=100)
    max_scheduled = models.IntegerField(blank=True, null=True)
    timeout = models.IntegerField(default=1800)
    max_tries = models.IntegerField(default=3)
    cpus_required = models.FloatField(default=1.0)
    mem_const_required = models.FloatField(default=64.0)
    mem_mult_required = models.FloatField(default=0.0)
    shared_mem_required = models.FloatField(default=0.0)
    disk_out_const_required = models.FloatField(default=64.0)
    disk_out_mult_required = models.FloatField(default=0.0)
    custom_resources = django.contrib.postgres.fields.JSONField(default=dict)

    icon_code = models.CharField(max_length=20, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    archived = models.DateTimeField(blank=True, null=True)
    paused = models.DateTimeField(blank=True, null=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = JobTypeManager()

    def convert_custom_resources(self):
        """Takes the raw custom_resources dict and performs the Scale version conversion on it so that the REST API
        always returns the latest version of the JSON schema

        :returns: The custom resources dict converted to the latest version of the JSON schema
        :rtype: dict
        """

        return self.get_custom_resources().get_dict()

    def get_custom_resources(self):
        """Returns the custom resources required for jobs of this type

        :returns: The custom resources
        :rtype: :class:`node.resources.json.resources.Resources`
        """

        return Resources(self.custom_resources)

    def get_job_interface(self):
        """Returns the interface for running jobs of this type

        :returns: The job interface for this type
        :rtype: :class:`job.configuration.interface.job_interface.JobInterface`
        """

        return JobInterface(self.interface)

    def get_error_interface(self):
        """Returns the interface for mapping a job's exit code or
        stderr/stdout expression to an error type"""

        return ErrorInterface(self.error_mapping)

    def get_job_configuration(self):
        """Returns default job configuration for this job type

        :returns: The default job configuration for this job type
        :rtype: :class:`job.configuration.json.job.job_config.JobConfiguration`
        """

        return JobConfiguration(self.configuration)

    def get_resources(self):
        """Returns the resources required for jobs of this type

        :returns: The required resources
        :rtype: :class:`node.resources.node_resources.NodeResources`
        """

        resources = Resources(self.custom_resources).get_node_resources()
        resources.remove_resource('cpus')
        resources.remove_resource('mem')
        resources.remove_resource('disk')
        cpus = max(self.cpus_required, MIN_CPUS)
        resources.add(NodeResources([Cpus(cpus)]))
        return resources

    def get_secrets_key(self):
        """Returns the reference key for job type secrets stored in the secrets backend.

        :returns: The job_type name and version concatenated
        :rtype: str
        """

        return '-'.join([self.name, self.version]).replace('.', '_')

    def natural_key(self):
        """Django method to define the natural key for a job type as the
        combination of name and version

        :returns: A tuple representing the natural key
        :rtype: tuple(string, string)
        """
        return self.name, self.version

    class Meta(object):
        """meta information for the db"""
        db_table = 'job_type'
        unique_together = ('name', 'version')


class JobTypeRevisionManager(models.Manager):
    """Provides additional methods for handling job type revisions
    """

    def create_job_type_revision(self, job_type):
        """Creates a new revision for the given job type. The job type's interface and revision number must already be
        updated. The caller must have obtained a lock using select_for_update() on the given job type model.

        :param job_type: The job type
        :type job_type: :class:`job.models.JobType`
        """

        new_rev = JobTypeRevision()
        new_rev.job_type = job_type
        new_rev.revision_num = job_type.revision_num
        new_rev.interface = job_type.interface
        new_rev.save()

    def get_by_natural_key(self, job_type, revision_num):
        """Django method to retrieve a job type revision for the given natural key

        :param job_type: The job type
        :type job_type: :class:`job.models.JobType`
        :param revision_num: The revision number
        :type revision_num: int
        :returns: The job type revision defined by the natural key
        :rtype: :class:`job.models.JobTypeRevision`
        """

        return self.get(job_type_id=job_type.id, revision_num=revision_num)

    def get_revision(self, job_type_id, revision_num):
        """Returns the revision for the given job type and revision number

        :param job_type_id: The ID of the job type
        :type job_type_id: int
        :param revision_num: The revision number
        :type revision_num: int
        :returns: The revision
        :rtype: :class:`job.models.JobTypeRevision`
        """

        return JobTypeRevision.objects.get(job_type_id=job_type_id, revision_num=revision_num)


class JobTypeRevision(models.Model):
    """Represents a revision of a job type. New revisions are created when the interface of a job type changes. Any
    inserts of a job type revision model requires obtaining a lock using select_for_update() on the corresponding job
    type model.

    :keyword job_type: The job type for this revision
    :type job_type: :class:`django.db.models.ForeignKey`
    :keyword revision_num: The number for this revision, starting at one
    :type revision_num: :class:`django.db.models.IntegerField`
    :keyword interface: The JSON interface for this revision of the job type
    :type interface: :class:`django.contrib.postgres.fields.JSONField`
    :keyword created: When this revision was created
    :type created: :class:`django.db.models.DateTimeField`
    """

    job_type = models.ForeignKey('job.JobType', on_delete=models.PROTECT)
    revision_num = models.IntegerField()
    interface = django.contrib.postgres.fields.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

    objects = JobTypeRevisionManager()

    def get_job_interface(self):
        """Returns the job type interface for this revision

        :returns: The job type interface for this revision
        :rtype: :class:`job.configuration.interface.job_interface.JobInterface`
        """

        return JobInterface(self.interface)

    def natural_key(self):
        """Django method to define the natural key for a job type revision as the combination of job type and revision
        number

        :returns: A tuple representing the natural key
        :rtype: tuple(string, int)
        """

        return self.job_type, self.revision_num

    class Meta(object):
        """meta information for the db"""
        db_table = 'job_type_revision'
        unique_together = ('job_type', 'revision_num')


class TaskUpdate(models.Model):
    """Represents a status update received for a task

    :keyword job_exe: The job execution that the task belongs to
    :type job_exe: :class:`django.db.models.ForeignKey`
    :keyword task_id: The task ID
    :type task_id: :class:`django.db.models.CharField`
    :keyword status: The status of the task
    :type status: :class:`django.db.models.CharField`

    :keyword timestamp: When the status update occurred (may be None)
    :type timestamp: :class:`django.db.models.DateTimeField`
    :keyword source: An optional source of the task status update
    :type source: :class:`django.db.models.CharField`
    :keyword reason: An optional reason for the task status update
    :type reason: :class:`django.db.models.CharField`
    :keyword message: An optional message related to the task status update
    :type message: :class:`django.db.models.TextField`

    :keyword created: When the task update was saved in the database
    :type created: :class:`django.db.models.DateTimeField`
    """

    job_exe = models.ForeignKey('job.JobExecution', on_delete=models.PROTECT)
    task_id = models.CharField(max_length=250)
    status = models.CharField(max_length=250)

    timestamp = models.DateTimeField(blank=True, null=True)
    source = models.CharField(blank=True, max_length=250, null=True)
    reason = models.CharField(blank=True, max_length=250, null=True)
    message = models.TextField(blank=True, null=True)

    created = models.DateTimeField(auto_now_add=True)

    class Meta(object):
        """Meta information for the database"""
        db_table = 'task_update'
