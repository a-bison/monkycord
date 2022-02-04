#
# Core implementation of the job system. Includes the main JobQueue and
# JobCron tasks, as well as related tools.
#

from abc import ABC, abstractmethod
from datetime import datetime, MINYEAR, MAXYEAR
import asyncio
import calendar
import collections
import copy
import functools
import logging
import time


#########
# UTILS #
#########

class CountingIdGenerator:
    def __init__(self, start_count=0):
        self.count = start_count

    def next_id(self):
        n = self.count
        self.count += 1

        return n


############
# JOB BASE #
############

# Container class for job information. Primary data class for this module.
class Job:
    def __init__(self, header, task):
        self.header = header
        self.task = task
        self.complete_event = asyncio.Event()

    # Marks this job as complete, notifies all coroutines waiting on it.
    def mark_complete(self):
        self.complete_event.set()

    # Wait for this job to finish.
    async def wait(self):
        await self.complete_event.wait()


# Task base class. Subclass this to create your own Tasks.
class JobTask(ABC):
    @abstractmethod
    async def run(self, header):
        raise NotImplemented("Subclass JobTask to implement specific tasks.")

    # Optional function that returns a default dict of properties to be passed
    # into run() on job execution. If None is returned, no defaults will be
    # set. Optionally, the implementation may use the current properties to
    # select new defaults.
    @classmethod
    def property_default(cls, properties):
        return None

    # Class method that returns the string name of this task type.
    @classmethod
    @abstractmethod
    def task_type(cls):
        return "NONE"

    # Pretty print info about this task. This should only return information
    # included in the header.properties dictionary. Higher level information
    # is the responsibility of the caller.
    def display(self, header):
        msg = "display() not implemented for task {}"
        logging.warning(msg.format(header.task_type))

        return ""


# Metadata for tracking a job, for scheduling and persistence purposes.
class JobHeader:
    @classmethod
    def from_dict(cls, id, d):
        return JobHeader(
            id,  # ignore loaded id
            d["task_type"],
            d["properties"],
            d["owner_id"],
            d["guild_id"],
            d["start_time"],
            d["schedule_id"]
        )

    def __init__(self, id, task_type, properties, owner_id, guild_id, start_time, schedule_id=None):
        self.id = id

        # Arguments to the job.
        self.properties = properties

        # Integer ID of the schedule that spawned this job. If no schedule ID,
        # then this will be None/null.
        self.schedule_id = schedule_id

        # Member ID of the discord user that started this job.
        # If this job was started by a schedule, this will reflect the owner of
        # the schedule.
        self.owner_id = owner_id

        # Guild ID of the guild this job was started in
        self.guild_id = guild_id

        # The date the job was started, as a unix timestamp, in UTC time.
        self.start_time = start_time

        # The task type string.
        self.task_type = task_type

        # Is the job cancelled?
        self.cancel = False

    def as_dict(self):
        return {
            "id": self.id,
            "properties": self.properties,
            "schedule_id": self.schedule_id,
            "owner_id": self.owner_id,
            "guild_id": self.guild_id,
            "start_time": self.start_time,
            "task_type": self.task_type
        }


# Base JobFactory functionality.
class JobFactory(ABC):
    def __init__(self, task_registry):
        self.id_counter = CountingIdGenerator()
        self.task_registry = task_registry

    # Get the next available job ID.
    def next_id(self):
        return self.id_counter.next_id()

    # Create a jobheader from a schedule entry
    async def create_jobheader_from_cron(self, cron):
        return cron.as_jobheader(
            self.next_id(),
            int(time.time())
        )

    # Create a jobheader from an existing dictionary
    async def create_jobheader_from_dict(self, header):
        return JobHeader.from_dict(
            self.next_id(),
            header
        )

    # Create a job using just the header.
    async def create_job_from_jobheader(self, header):
        task = await self.create_task(header)
        j = Job(header, task)

        # Update header.properties with declared task defaults, if any.
        defaults = task.property_default(header.properties)
        if defaults:
            new_props = dict(defaults)
            new_props.update(header.properties)
            header.properties = new_props

        return j

    # Create a job from a schedule entry
    async def create_job_from_cron(self, cron):
        header = await self.create_jobheader_from_cron(cron)
        return await self.create_job_from_jobheader(header)

    # Create a job from an existing dictionary (typically loaded from cfg)
    async def create_job_from_dict(self, header):
        header = await self.create_jobheader_from_dict(header)
        return await self.create_job_from_jobheader(header)

    # Create a new task using a jobheader.
    @abstractmethod
    async def create_task(self, header):
        pass


# Simple registry for getting task types.
class TaskRegistry:
    def __init__(self):
        self.tasks = {}

    def register(self, cls):
        if not hasattr(cls, "task_type"):
            raise TypeError("Task class must have task_type classmethod")

        self.tasks[cls.task_type()] = cls

    def get(self, name):
        if isinstance(name, str):
            return self.tasks[name]
        elif isinstance(name, type) and issubclass(name, JobTask):
            return name
        else:
            raise TypeError("Object {} has invalid type".format(str(name)))

    # Forces a passed tasktype object to be a string. Also serves to validate
    # a task_type string.
    def force_str(self, tasktype):
        return self.get(tasktype).task_type()

    # Tests if a task type string is in the task registry.
    def __contains__(self, item):
        return item in self.tasks


# A single job queue. Can run one job at a time.
class JobQueue:
    def __init__(self, eventloop=None):
        if eventloop is None:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = eventloop

        self.active_job = None
        self.active_task = None
        self.job_queue = asyncio.Queue()

        # Job dict used for display purposes, because asyncio.Queue doesn't
        # support peeking
        self.jobs = collections.OrderedDict()

        self.job_submit_callback = None
        self.job_start_callback = None
        self.job_stop_callback = None
        self.job_cancel_callback = None

    async def submit_job(self, job):
        if self.job_submit_callback is not None:
            await self.job_submit_callback(job.header)

        await self.job_queue.put(job)
        self.jobs[job.header.id] = job

    def on_job_submit(self, callback):
        self.job_submit_callback = callback

    def on_job_start(self, callback):
        self.job_start_callback = callback

    def on_job_stop(self, callback):
        self.job_stop_callback = callback

    def on_job_cancel(self, callback):
        self.job_cancel_callback = callback

    def _rm_job(self, job):
        if job is None:
            return

        if job.header.id in self.jobs:
            del self.jobs[job.header.id]

        self.active_job = None
        self.active_task = None

    async def run(self):
        logging.info("Starting job queue...")

        try:
            while True:
                await self.mainloop()
        except:
            logging.exception("Job queue stopped unexpectedly!")
        finally:
            logging.info("Job queue stoppped.")

    async def mainloop(self):
        j = await self.job_queue.get()

        if j.header.cancel:
            logging.info("Skipping cancelled job " + str(j.header.id))
            self._rm_job(self.active_job)
            return

        logging.info("Start new job " + str(j.header.as_dict()))
        self.active_job = j

        # Schedule task
        coro = self.active_job.task.run(self.active_job.header)
        task = self.loop.create_task(coro)
        self.active_task = task

        if self.job_start_callback:
            await self.job_start_callback(j.header)

        try:
            await task
        except asyncio.CancelledError:
            logging.warning("Uncaught CancelledError in job " + str(j.header.id))
        except:
            logging.exception("Got exception while running job")
        finally:
            if self.active_job:
                # Notify any listeners on this job that it's done.
                self.active_job.mark_complete()

            self._rm_job(self.active_job)

        if self.job_stop_callback:
            await self.job_stop_callback(j.header)

    async def canceljob(self, job):
        if isinstance(job, Job):
            job = job.header.id

        self.jobs[job].header.cancel = True

        if self.active_job.header.id == job and self.active_task:
            self.active_task.cancel()

        if self.job_cancel_callback:
            await self.job_cancel_callback(self.jobs[job].header)

        del self.jobs[job]


##################
# JOB SCHEDULING #
##################

SCHED_PARSE_POSITIONS = [
    "minute",
    "hour",
    "dayofmonth",
    "month",
    "dayofweek"
]
SCHED_PARSE_LIMITS = [
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 6)
]
SCHED_LIMITS = {k: v for k, v in zip(SCHED_PARSE_POSITIONS, SCHED_PARSE_LIMITS)}
SCHED_WD_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6
}
SCHED_MACROS = {
    "!weekly": "* * SUN",
    "!monthly": "1 * *",
    "!yearly": "1 1 *",
    "!daily": "* * *"
}


# Main scheduling data class.
class CronHeader:
    @classmethod
    def from_dict(cls, d):
        return CronHeader(**d)

    def __init__(self, id, task_type, properties, owner_id, guild_id, schedule):
        # ID of this schedule. NOTE: Unlike Jobs, whose ID count resets after
        # every startup, schedules always have the same IDs.
        self.id = id

        # Arguments to the jobs created by this schedule.
        self.properties = properties

        # The task type string.
        self.task_type = task_type

        # Member ID of the discord user that owns this schedule.
        self.owner_id = owner_id

        # Guild ID of the guild this schedule was created in.
        self.guild_id = guild_id

        # Schedule string for this schedule. Determines the time that this
        # job will run at. The header stores the schedule string in the exact
        # same format as the unix cron utility.
        #
        # min hour day_of_month month day_of_week
        # note: day_of_week runs from 0-6, Sunday-Saturday.
        #
        # supported operators:
        # * - Signifies all possible values in a field.
        #
        # example:
        # 1 4 * * 0 - run the job at 4:01 am every Sunday.
        self.schedule = schedule

        # RUNTIME VALUES
        # These values are generated at runtime and are never saved.

        # A python datetime object representing the next time this schedule
        # will run. Used by a schedule dispatcher to avoid missing a job fire.
        self.next = None

    def as_dict(self):
        return {
            "id": self.id,
            "properties": self.properties,
            "task_type": self.task_type,
            "owner_id": self.owner_id,
            "guild_id": self.guild_id,
            "schedule": self.schedule
        }

    def as_jobheader(self, id, start_time):
        return JobHeader(
            id,
            self.task_type,
            self.properties,
            self.owner_id,
            self.guild_id,
            start_time,
            self.id
        )

    # Updates self.next to the next run after current datetime. Used by the
    # schedule dispatcher.
    def update_next(self):
        sched_obj = cron_parse(self.schedule)

        # Make sure to avoid multiple schedule firings, so make carry=1.
        # See cron_next_date() for more details.
        self.next = cron_next_date_as_datetime(sched_obj, carry=1)

    def match(self, **kwargs):
        d = self.as_dict()

        for key, value in kwargs.items():
            if key not in d:
                raise TypeError("Cannot use {} in CronHeader.match".format(
                    key
                ))

            if d[key] != value:
                return False

        return True


class ScheduleParseException(Exception):
    def __init__(self, *args, cronstr=None):
        super().__init__(*args)

        self.cronstr = cronstr


# Parse a schedule string into a dictionary.
@functools.cache
def cron_parse(schedule_str):
    schedule_str = schedule_str.lower()

    # Parse macros first
    for macro, repl in SCHED_MACROS.items():
        schedule_str = schedule_str.replace(macro, repl)

    s_split = schedule_str.lower().split()
    s_dict = {}

    if len(s_split) < 5:
        raise ScheduleParseException("less than 5 elements", cronstr=schedule_str)
    elif len(s_split) > 5:
        raise ScheduleParseException("more than 5 elements", cronstr=schedule_str)

    for limit, name, elem, i in zip(SCHED_PARSE_LIMITS, SCHED_PARSE_POSITIONS, s_split, range(5)):
        lower, upper = limit

        if elem == "*":
            s_dict[name] = None
            continue

        try:
            result = int(elem)
        except ValueError:
            if name == "dayofweek" and elem.lower() in SCHED_WD_NAMES:
                result = SCHED_WD_NAMES[elem.lower()]
            else:
                msg = "position {}({}): {} is not an integer"
                raise ScheduleParseException(
                    msg.format(i, name, elem),
                    cronstr=schedule_str
                )

        # Verify item range
        if result < lower or result > upper:
            msg = "position {}({}): {} outside bounds {}>{}>{}"
            raise ScheduleParseException(
                msg.format(i, name, elem, lower, elem, upper),
                cronstr=schedule_str
            )

        s_dict[name] = result

    return s_dict


# Conversion functions for weekday formats. The python datetime library
# starts counting weekdays at Mon=0, but cron strings start at Sun=0.
def wd_cron_to_python(wd):
    return (wd + 6) % 7


def wd_python_to_cron(wd):
    return (wd + 1) % 7


# Test whether a schedule should run, based on a timedate object.
def cron_match(schedule_str, timedate_obj):
    sd = cron_parse(schedule_str)

    for name, elem in sd.items():
        # Skip *'s
        if elem is None:
            continue

        # If we encounter a field that doesn't match, stop and return False.
        # Therefore, if all specified fields match, we will return True.
        if ((name == "minute"     and elem != timedate_obj.minute) or
            (name == "hour"       and elem != timedate_obj.hour) or
            (name == "dayofmonth" and elem != timedate_obj.day) or
            (name == "month"      and elem != timedate_obj.month) or
            (name == "dayofweek"  and elem != wd_python_to_cron(timedate_obj.weekday()))):
            return False

    return True


# From a cron structure parsed from cron_parse, determine what the next
# date will be the scheduled job will run, based on current date. Returns a dict:
#
# {
#   "minute": ...,
#   "hour":   ...,
#   "day":    ...,
#   "month":  ...,
#   "year":   ...
# }
#
# If carry is supplied, one minute will be added to the from_date. This can
# help avoid multiple schedule firings if the from_date already matches the
# schedule.
def cron_next_date(schedule, from_date=None, carry=0):
    # Make a copy so we can freely modify.
    schedule = dict(schedule)

    if from_date is not None:
        current_date = from_date
    else:
        current_date = datetime.now()

    next_date = {
        "minute": current_date.minute,
        "hour": current_date.hour,
        "dayofmonth": current_date.day,
        "month": current_date.month,
        "year": current_date.year
    }

    if isinstance(schedule["dayofmonth"], int):
        schedule["dayofmonth"] = [schedule["dayofmonth"]]

    # Copy original in case we need to recalculate dayofmonth for month/year
    # changes.
    if schedule["dayofmonth"] is not None:
        schedule["_orig_dayofmonth"] = list(schedule["dayofmonth"])
    else:
        schedule["_orig_dayofmonth"] = []

    next_date["minute"], carry = _next_elem("minute", next_date["minute"],
                                            carry, next_date, schedule)
    next_date["hour"], carry = _next_elem("hour", next_date["hour"],
                                          carry, next_date, schedule)

    # Day of month is tricky. If there was a carry, that means we flipped
    # to the next month, and potentially the next year. If that's the case,
    # then we need to do a second round.
    new_day, carry = _cron_next_day(
        schedule,
        carry,
        day=next_date["dayofmonth"],
        month=next_date["month"],
        year=next_date["year"]
    )

    # Only do another round if dayofweek is present.
    # TODO Evaluate leap year edge case. May need to do another round
    # regardless of dayofweek.
    if carry > 0 and schedule["dayofweek"] is not None:
        # We flipped month, so do another round, starting at the first day
        # of the month. la = lookahead
        month_la = next_date["month"]
        year_la = next_date["year"]

        logging.info("cron_next_date(): month overrun, recalc day")

        if month_la == 12:
            month_la = 1
            year_la += 1
            logging.info("cron_next_date(): year overrun")
        else:
            month_la += 1

        new_day, extra_carry = _cron_next_day(
            schedule, 0,
            day=1,
            month=month_la,
            year=year_la
        )

        # FIXME This will fall over in the case that the day rolls over twice,
        # which could happen if a day falls on a number beyond the new month.
        if extra_carry > 0:
            raise Exception("Could not recalculate dayofmonth")

    # FIXME Possible bug: if month rolls over after previous calculation,
    # days need to be recalculated, but this doesn't happen.
    next_date["dayofmonth"] = new_day
    next_date["month"], carry = _next_elem("month", next_date["month"],
                                           carry, next_date, schedule)

    # Don't need full elem calculation for year, so just bump it if there
    # was a carry.
    next_date["year"] += carry

    return next_date


def _cron_next_day(schedule, carry, day, month, year):
    # If dayofweek is present, fold it into dayofmonth to make things
    # easier to calculate.
    if schedule["dayofweek"] is not None:
        weekdays = cron_calc_days(
            year,
            month,
            schedule["dayofweek"]
        )

        schedule["dayofmonth"] = sorted(set(schedule["_orig_dayofmonth"] + weekdays))

    newday, carry = _next_elem(
        "dayofmonth",
        day,
        carry,
        {"year": year, "month": month},
        schedule
    )

    return newday, carry


# Calculate the upper and lower bounds for a given element.
def _limit_elem(elem_name, t):
    if elem_name == "dayofmonth":
        upper = calendar.monthrange(t["year"], t["month"])[1]
        return 1, upper

    elif elem_name == "year":
        return MINYEAR, MAXYEAR

    else:
        return SCHED_LIMITS[elem_name]


# Calculate the next element
def _next_elem(elem_name, elem, carry, t, schedule):
    sched_elem = schedule[elem_name]
    lower, upper = _limit_elem(elem_name, t)

    new_elem = elem + carry
    new_carry = 0

    logging.info("next_elem(): {}: {}({}) -> {}".format(
        elem_name,
        elem,
        new_elem,
        str(sched_elem)
    ))

    # If our sched element can be anything, don't touch it. Note that
    # the carry has already been taken into account. We just need to check
    # whether the carry made this element roll over.
    if sched_elem is None:
        if new_elem > upper:
            new_elem = lower
            new_carry = 1

        return new_elem, new_carry

    # Otherwise, select the next available schedule slot for this element.
    # If no slot could be selected, select the first one, and carry.
    no_elem_found = False
    if isinstance(sched_elem, int):
        sched_elem = [sched_elem]
    sched_elem = sorted(sched_elem)
    for i in sched_elem:
        if i < new_elem:
            continue
        else:
            new_elem = i
            break
    else:
        no_elem_found = True

    # If we couldn't find the next element, or the new element that WAS
    # selected goes over the given limit, roll back around and carry.
    if new_elem > upper or no_elem_found:
        new_elem = sched_elem[0]
        new_carry = 1

    return new_elem, new_carry


# Calculate all of a given weekday in a given month. Returns
# a list of day numbers within the given month.
@functools.cache
def cron_calc_days(year, month, wd):
    # Calendar starts at 0 = Monday, goes to 6 = Sunday. Cron format is offset
    # from that, so we need to convert to python range.
    wd = wd_cron_to_python(wd)
    c = calendar.Calendar()
    return [d for d, _wd in c.itermonthdays2(year, month)
            if d != 0 and _wd == wd]


# Convert the output of cron_next_date to a datetime object.
def cron_next_to_datetime(cron_next):
    return datetime(
        cron_next["year"],
        cron_next["month"],
        cron_next["dayofmonth"],
        cron_next["hour"],
        cron_next["minute"]
    )


# Get next date as datetime.
def cron_next_date_as_datetime(schedule, from_date=None, carry=0):
    return cron_next_to_datetime(cron_next_date(schedule, from_date, carry))


# A scheduler that starts jobs at specific real-world dates.
# Expected to have minute-level accuracy.
class JobCron:
    def __init__(self, jobqueue, jobfactory):
        self.jobqueue = jobqueue
        self.jobfactory = jobfactory

        self.schedule_lock = asyncio.Lock()
        self.schedule = {}

        self.sched_create_callback = None
        self.sched_delete_callback = None

    def on_create_schedule(self, callback):
        self.sched_create_callback = callback

    def on_delete_schedule(self, callback):
        self.sched_delete_callback = callback

    # Stop a schedule from running.
    async def delete_schedule(self, id):
        async with self.schedule_lock:
            sheader = self.schedule[id]
            sheader.next = None

            if self.sched_delete_callback is not None:
                await self.sched_delete_callback(sheader)

            del self.schedule[id]

    # Replace a schedule entry with a new one.
    async def replace_schedule(self, id, sheader):
        async with self.schedule_lock:
            old_hdr = self.schedule[id]

            new_hdr = sheader
            new_hdr.next = None
            new_hdr.update_next()

            # Call both delete and create callbacks to ensure any
            # external state is updated properly
            if self.sched_delete_callback is not None:
                await self.sched_delete_callback(old_hdr)

            if self.sched_create_callback is not None:
                await self.sched_create_callback(new_hdr)

            self.schedule[id] = new_hdr

    # Schedule a job.
    async def create_schedule(self, sheader):
        async with self.schedule_lock:
            # Calculate the next run date right away. This also
            # functions to validate the cron str before scheduling.
            sheader.update_next()

            logging.info("New schedule created: " + str(sheader.as_dict()))
            if self.sched_create_callback is not None:
                await self.sched_create_callback(sheader)

            self.schedule[sheader.id] = sheader

    async def run(self):
        # The background task that starts jobs. Checks if there are new jobs
        # to start roughly once every minute.
        logging.info("Starting job scheduler...")

        try:
            while True:
                await asyncio.sleep(60)

                # Do not allow modifications to the schedule while a schedule
                # check is running.
                async with self.schedule_lock:
                    await self.mainloop()
        except:
            logging.exception("Scheduler stopped unexpectedly!")

    # Single iteration of schedule dispatch.
    async def mainloop(self):
        for id, sheader in self.schedule.items():
            # If we've gone past the scheduled time, fire the job,
            # regenerate the next time using the cron string.
            if sheader.next and sheader.next < datetime.now():
                sheader.update_next()
                await self._start_scheduled_job(sheader)

    async def _start_scheduled_job(self, cron_header):
        job = await self.jobfactory.create_job_from_cron(cron_header)
        msg = "SCHED {}: Firing job type={} {}"
        logging.info(msg.format(
            cron_header.id, job.header.task_type, job.task.display(job.header)
        ))
        await self.jobqueue.submit_job(job)

        return job

    # Run a scheduled job immediately, returning the resulting job.
    async def run_now(self, id):
        hdr = self.schedule[id]
        return await self._start_scheduled_job(hdr)

    # Returns a copy of the schedule, filtered by the given parameters.
    def sched_filter(self, **kwargs):
        return {id: c for id, c in self.schedule.items()
                if c.match(**kwargs)}

    # Returns a copy of the schedule.
    def sched_copy(self):
        return dict(self.schedule)

    # Reschedule a schedule entry.
    async def reschedule(self, id, cronstr):
        hdr = self.schedule[id]
        
        new_hdr = copy.deepcopy(hdr)
        new_hdr.schedule = cronstr

        await self.replace_schedule(id, new_hdr)


##################
# BUILT IN TASKS #
##################

# A task that does nothing but sleep for a given time. Mostly used for
# debugging purposes. For example, you can fill the job queue with BlockerTasks
# that never end to test proper queue and cancellation behavior.
class BlockerTask(JobTask):
    # Ignore any arguments passed in to retain compatibility with all job
    # factories.
    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def task_type(cls):
        return "blocker"

    @classmethod
    def property_default(cls, properties):
        return {
            "time": 60  # seconds. if None, loops forever.
        }

    async def run(self, header):
        p = header.properties
        time = p["time"]

        if time is None:
            while True: 
                await asyncio.sleep(1)
        else:
            counter = time

            while counter > 0:
                await asyncio.sleep(1)
                counter -= 1

    def display(self, header):
        return ""
