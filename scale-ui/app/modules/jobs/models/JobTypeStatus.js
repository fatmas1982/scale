(function () {
    'use strict';

    angular.module('scaleApp').factory('JobTypeStatus', function (scaleConfig, JobType, JobExecution) {
        var JobTypeStatus = function (job_type, job_counts) {
            this.job_type = JobType.transformer(job_type);
            this.job_counts = job_counts;
            this.has_running = _.find(job_counts, 'status', 'RUNNING');
            this.description = this.getPerformance().rateDescription;
        };

        // public methods
        JobTypeStatus.prototype = {
            toString: function () {
                return 'JobTypeStatus';
            },
            getPerformance: function () {
                var failedArr = _.sortByOrder(_.filter(this.job_counts, { status: 'FAILED' }), ['count'], ['desc']),
                    completed = _.find(this.job_counts, 'status', 'COMPLETED') || { count: 0 },
                    failed = _.sum(failedArr, 'count'),
                    failedCategory = failedArr.length > 0 ? failedArr[0].category : '',
                    total = failedArr.length > 0 ? failed + completed.count : completed.count,
                    successRate = total === 0 ? 0 : 100 - ((failed / total) * 100).toFixed(2),
                    successRateDescription = 'success';

                if (successRate <= 30 && total > 0) {
                    successRateDescription = 'error';
                } else if (successRate > 30 && successRate <= 60 && total > 0) {
                    successRateDescription = 'warning';
                } else if (total === 0 && !this.has_running) {
                    successRateDescription = 'z_inactive'; // prepend with 'z_' for ordering purposes
                }

                return {
                    rate: successRate,
                    rateDescription: successRateDescription,
                    failed: failed,
                    failedCategory: failedCategory,
                    completed: completed.count,
                    total: total
                };
            },
            getRunning: function () {
                return _.find(this.job_counts, 'status', 'RUNNING') || { count: 0 };
            },
            getFailures: function () {
                var failed = _.where(this.job_counts, { 'status': 'FAILED' }),
                    failedValues = _.values(_.groupBy(failed, 'category'));

                var getFailureCounts = function (categories) {
                    var returnArr = [];
                    _.forEach(categories, function (category) {
                        _.forEach(category, function (val) {
                            returnArr.push({ status: val.category, count: val.count });
                        });
                    });
                    return _.sortByOrder(returnArr, ['count'], ['desc']);
                };

                return getFailureCounts(failedValues);
            },
            getCellFill: function () {
                var status = this.getPerformance();
                if (status.failedCategory === 'SYSTEM') {
                    return scaleConfig.colors.failure_system;
                } else if (status.failedCategory === 'DATA') {
                    return scaleConfig.colors.failure_data;
                } else if (status.failedCategory === 'ALGORITHM') {
                    return scaleConfig.colors.failure_algorithm;
                } else {
                    if (status.rateDescription === 'z_inactive') {
                        return scaleConfig.colors.chart_gray_dark;
                    }
                }
                return scaleConfig.colors.chart_green;
            },
            getCellActivity: function () {
                var running = this.getRunning();
                if (running.count > 0) {
                    return '&#x' + scaleConfig.activityIconCode + ';';
                }
                return '';
            },
            getCellActivityTotal: function () {
                return this.getRunning().count > 0 ? this.getRunning().count : '';
            },
            getCellError: function () {
                var performance = this.getPerformance();
                return 'Failed: ' + (performance.failed);
            },
            getCellTotal: function () {
                var performance = this.getPerformance();
                return 'Completed: ' + performance.completed;
            },
            getCellPauseResume: function () {
                return;
            },
            getCellFailures: function () {
                return _.map(this.getFailures(), 'status');
            }
        };

        // static methods, assigned to class
        JobTypeStatus.build = function (data) {
            if (data) {
                return new JobTypeStatus(
                    data.job_type,
                    data.job_counts
                );
            }
            return new JobTypeStatus();
        };

        JobTypeStatus.transformer = function (data) {
            if (angular.isArray(data)) {
                return data
                    .map(JobTypeStatus.build)
                    .filter(Boolean);
            }
            return JobTypeStatus.build(data);
        };

        return JobTypeStatus;
    });
})();
