import discord
from discord.ext import commands

from datetime import datetime
import asyncio
import inspect
import json
import logging
import time
import typing

from . import job
from . import config
from . import util
from .exception import NotAdministrator
from .util import ack


# High level interface to the bot core.
# Automatically links together cfg and job systems, and subscribes to
# discord events.
class CoreWrapper:
    def __init__(self, bot, config_path, cfgtemplate, common_cfgtemplate):
        self.cfgtemplate = dict(cfgtemplate)
        self.cfgtemplate.update({
            "jobs": {},
            "cron": {}
        })

        self.bot = bot

        self.common_cfgtemplate = dict(common_cfgtemplate)
        self.common_cfgtemplate.update({
            # save the last schedule id, so we don't overlap new schedules
            # with old ones
            "last_schedule_id": 0
        })

        self.config_db = config.JsonConfigDB(
            config_path,
            template=self.cfgtemplate,
            main_template=self.common_cfgtemplate
        )

        # Task registry
        self.task_registry = job.TaskRegistry()

        # Job executor/consumer component
        self.jobqueue = job.JobQueue(self.bot.loop)
        self.jobqueue.on_job_submit(self._cfg_job_create)
        self.jobqueue.on_job_stop(self._cfg_job_delete)
        self.jobqueue.on_job_cancel(self._cfg_job_delete)
        self.jobfactory = DiscordJobFactory(self.task_registry, self.bot)
        self.jobtask = None

        # Job scheduler component
        self.jobcron = job.JobCron(self.jobqueue, self.jobfactory)
        self.jobcron.on_create_schedule(self._cfg_sched_create)
        self.jobcron.on_delete_schedule(self._cfg_sched_delete)
        self.cronfactory = DiscordCronFactory(
            self.task_registry,
            self.config_db.get_common_config().opts["last_schedule_id"] + 1
        )
        self.crontask = None

        # Register discord events
        # TODO On guild leave
        self.bot.add_listener(self.on_guild_join, 'on_guild_join')
        self.bot.add_listener(self.on_ready, 'on_ready')

        self.jobs_resumed = False

        # Create job consumer and scheduler
        loop = self.bot.loop
        self.jobtask = loop.create_task(self.jobqueue.run())
        self.crontask = loop.create_task(self.jobcron.run())

    ##################
    # EVENT HANDLERS #
    ##################

    # Resume all jobs that never properly finished from the last run.
    # Called from on_ready() to ensure that all discord state is init'd
    # properly
    async def resume_jobs(self):
        for guild_id, cfg in self.config_db.db.items():
            jobs = cfg.sub("jobs").get_and_clear()

            for job_id, job_header in jobs.items():
                await self.resume_job(job_header)

            msg = "Resumed {} unfinished job(s) in guild {}"
            logging.info(msg.format(len(jobs), guild_id))

    # Resume job from a loaded job header dict.
    async def resume_job(self, header):
        job = await self.jobfactory.create_job_from_dict(header)
        await self.jobqueue.submit_job(job)

    # Reschedule all cron entries from cfg
    async def reschedule_all_cron(self):
        for guild_id, cfg in self.config_db.db.items():
            crons = cfg.sub("cron").get_and_clear()

            for sched_id, sched_header in crons.items():
                await self.reschedule_cron(sched_header)

            msg = "Loaded {} schedule(s) in guild {}"
            logging.info(msg.format(len(crons), guild_id))

    # Get the config object for a given job/cron header.
    def get_cfg_for_header(self, header):
        guild = self.bot.get_guild(header.guild_id)
        cfg = self.config_db.get_config(guild)

        return cfg

    async def reschedule_cron(self, header_dict):
        header = await self.cronfactory.create_cronheader_from_dict(header_dict)
        await self.jobcron.create_schedule(header)

    # When a job is submitted, create an entry in the config DB.
    async def _cfg_job_create(self, header):
        cfg = self.get_cfg_for_header(header)
        cfg.sub("jobs").set(str(header.id), header.as_dict())

    # Once a job is done, delete it from the config db.
    async def _cfg_job_delete(self, header):
        cfg = self.get_cfg_for_header(header)
        cfg.sub("jobs").delete(str(header.id), ignore_keyerror=True)

    # Add created schedules to the config DB, and increase the
    # last_schedule_id parameter.
    async def _cfg_sched_create(self, header):
        cfg = self.get_cfg_for_header(header)
        cfg.sub("cron").set(str(header.id), header.as_dict())

        common_cfg = self.config_db.get_common_config()
        common_cfg.get_and_set(
            "last_schedule_id",
            lambda val: max(val, header.id)
        )

    # Remove deleted schedules from the config DB.
    async def _cfg_sched_delete(self, header):
        cfg = self.get_cfg_for_header(header)
        cfg.sub("cron").delete(str(header.id))

    # Create configs for any guilds we were added to while offline
    async def join_guilds_offline(self):
        async for guild in self.bot.fetch_guilds():
            logging.info("In guilds: {}({})".format(guild.name, guild.id))
            _ = self.config_db.get_config(guild)

        self.config_db.write_db()

    async def on_ready(self):
        if not self.jobs_resumed:
            await self.reschedule_all_cron()
            await self.resume_jobs()
            self.jobs_resumed = True

        logging.info("Core ready.")

    # Add a new config slice if we join a new guild.
    async def on_guild_join(self, guild):
        logging.info("Joined new guild: {}({})".format(guild.name, guild.id))
        await self.config_db.create_config(guild)

    #########################
    # UTILITY / PASSTHROUGH #
    #########################

    def run(self, secret):
        loop = self.bot.loop

        try:
            # Perform initialization and log in
            loop.run_until_complete(self.bot.login(secret))
            loop.run_until_complete(self.join_guilds_offline())
            loop.run_until_complete(self.bot.connect())

        except KeyboardInterrupt:
            print("Keyboard Interrupt!")
        finally:
            loop.close()

    # Enqueue a new job. Returns the created job object.
    async def start_job(self, ctx, task_type, properties):
        job = await self.jobfactory.create_job(ctx, task_type, properties)
        await self.jobqueue.submit_job(job)

        return job

    # Schedule a job
    async def schedule_job(self, ctx, task_type, properties, cron_str):
        chdr = await self.cronfactory.create_cronheader(
            ctx,
            properties,
            task_type,
            cron_str
        )
        await self.jobcron.create_schedule(chdr)

        return chdr

    # Register a task class.
    def task(self, tsk):
        return self.task_registry.register(tsk)

    # Shortcut to get the config for a given command.
    # Also supports messages.
    def cfg(self, ctx):
        cfg = self.config_db.get_config(ctx.guild)
        return cfg


######################################
# JOB INFRASTRUCTURE IMPLEMENTATIONS #
######################################
# Discord-specific aspects of core.job.

# Implementation of discord-specific aspects of constructing job objects.
class DiscordJobFactory(job.JobFactory):
    def __init__(self, task_registry, bot):
        super().__init__(task_registry)
        self.bot = bot

    # Create a new jobheader.
    async def create_jobheader(self, ctx, properties, task_type, schedule_id):
        header = job.JobHeader(
            self.next_id(),
            task_type,
            properties,
            ctx.message.author.id,
            ctx.guild.id,
            int(time.time()),
            schedule_id
        )

        return header

    # Create a new job.
    async def create_job(self, ctx, task_type, properties, schedule_id=None):
        task_type = self.task_registry.force_str(task_type)
        header = await self.create_jobheader(ctx, properties, task_type, schedule_id)
        j = await self.create_job_from_jobheader(header)
        return j

    # OVERRIDE
    # Discord tasks take some extra constructor parameters, so we need to
    # construct those jobs through the DiscordJobFactory.
    async def create_task(self, header, guild=None):
        if guild is None:
            guild = self.bot.get_guild(header.guild_id)

        task_cls = self.task_registry.get(header.task_type)
        task = task_cls(self.bot, guild)

        return task


# Companion to the JobFactory. No core.job counterpart.
class DiscordCronFactory:
    def __init__(self, registry, start_id=0):
        self.task_registry = registry
        self.id_counter = job.CountingIdGenerator(start_id)

    async def create_cronheader(self, ctx, properties, task_type, cron_str):
        header = job.CronHeader(
            self.id_counter.next_id(),
            self.task_registry.force_str(task_type),
            properties,
            ctx.message.author.id,
            ctx.guild.id,
            cron_str
        )

        return header

    async def create_cronheader_from_dict(self, header_dict):
        return job.CronHeader.from_dict(header_dict)


#################
# DISCORD TASKS #
#################
# An assortment of JobTasks for Discord.

# Task that sends a discord message on a timer to a given channel.
class MessageTask(job.JobTask):
    MAX_MSG_DISPLAY_LEN = 15

    def __init__(self, bot, guild):
        self.bot = bot
        self.guild = guild

    async def run(self, header):
        p = header.properties
        channel = self.guild.get_channel(p["channel"])

        for _ in range(p["post_number"]):
            await channel.send(p["message"])
            await asyncio.sleep(p["post_interval"])

    @classmethod
    def task_type(cls):
        return "message"

    @classmethod
    def property_default(cls, properties):
        return {
            "message": "hello",
            "channel": 0,
            "post_interval": 1,  # SECONDS
            "post_number": 1
        }

    def display(self, header):
        p = header.properties
        msg = p["message"]

        if len(msg) > MessageTask.MAX_MSG_DISPLAY_LEN:
            msg = msg[0:MessageTask.MAX_MSG_DISPLAY_LEN] + "..."

        fmt = "message=\"{}\" post_interval={} post_number={}"

        return fmt.format(msg, p["post_interval"], p["post_number"])


########
# COGS #
########
# Cogs for use with the core. Implements some generic management and debugging
# behavior for administrators.

# Optional cog containing job management commands for testing/administration
# NOTE: This cog requires the Members intent.
class JobManagement(commands.Cog):
    def __init__(self, core):
        self.core = core

        self.bot = core.bot
        self.jq = core.jobqueue
        self.jc = core.jobcron
        self.registry = core.task_registry

    def pretty_print_job(self, job):
        h = job.header
        owner = self.bot.get_user(h.owner_id).name
        s = "{}: owner={} type={}".format(h.id, owner, h.task_type)

        if h.schedule_id is not None:
            s += " sched=" + str(h.schedule_id)

        task_str = job.task.display(job.header)

        if task_str:
            s += " " + task_str

        return s

    # Get jobs valid for a specific guild.
    def get_guild_jobs(self, guild):
        return {id: j for id, j in self.jq.jobs.items()
                if j.header.guild_id == guild.id}

    def job_can_modify(self, ctx, owner_id):
        return (
            ctx.author.guild_permissions.administrator or
            owner_id == ctx.author.id
        )

    @commands.group(aliases=["jobs"], invoke_without_command=True)
    async def job(self, ctx):
        """View or modify actively queued jobs."""
        await ctx.send("Specify a subcommand.")
        await ctx.send_help("job")

    # List jobs for a given guild.
    @job.command(name="list")
    async def joblist(self, ctx):
        """List enqueued jobs."""
        jobs = self.get_guild_jobs(ctx.guild)
        joblines = [self.pretty_print_job(j) for j in jobs.values()]

        if joblines:
            await ctx.send(util.codelns(joblines))
        else:
            await ctx.send("No jobs.")

    @job.command(name="raw")
    async def jobraw(self, ctx, id: int):
        """Print the internal representation of a job. Mostly for debugging."""
        jobs = self.get_guild_jobs(ctx.guild)

        if id in jobs:
            await ctx.send(util.codejson(jobs[id].header.as_dict()))
        else:
            await ctx.send("Job {} does not exist.".format(id))

    @job.command(name="cancel")
    async def jobcancel(self, ctx, id: int):
        """Cancel a job listed in the job list.

        Members may only cancel jobs they started. Users with the Administrator
        permission may cancel any job.
        """
        jobs = self.get_guild_jobs(ctx.guild)

        if id in jobs:
            if not self.job_can_modify(ctx, jobs[id].header.owner_id):
                raise NotAdministrator("cancel jobs started by other users")

            await self.jq.canceljob(id)
            await ack(ctx)
        else:
            await ctx.send("Job {} does not exist.".format(id))

    @job.command(name="canceluser")
    async def jobcanceluser(self, ctx, user: typing.Optional[discord.Member], *rest):
        """Cancel all jobs started by a specific user.

        Unless you're administrator, you'll only be able to cancel your
        own jobs.
        """
        corrected_user = await util.process_user_optional(ctx, user, rest)

        if corrected_user is None:
            return

        if not self.job_can_modify(ctx, corrected_user.id):
            raise NotAdministrator("cancel jobs started by other users")

        jobs = [j for _, j in self.get_guild_jobs(ctx.guild).items()
                if j.header.owner_id == corrected_user.id]

        if not jobs:
            await ctx.send("No jobs to delete.")
            return

        for j in reversed(jobs):
            await self.jq.canceljob(j.header.id)

        await ack(ctx)

    @job.command(name="cancelall")
    @util.check_administrator()
    async def jobcancelall(self, ctx):
        """Cancel all jobs. Only Administrators may use this command.
        """
        jobs = self.get_guild_jobs(ctx.guild)

        for id, jobs in reversed(jobs.items()):
            await self.jq.canceljob(id)

        await ack(ctx)

    @job.command("flush")
    @commands.is_owner()
    async def jobflush(self, ctx):
        """Cancel all jobs currently scheduled, across all servers.

        You must be the owner of the bot to use this command.
        """
        for id in reversed(self.jq.jobs.keys()):
            await self.jq.canceljob(id)

        await ack(ctx)

    def pretty_print_cron(self, cron):
        owner = self.bot.get_user(cron.owner_id).name

        s = "{}: owner={} type={} sched=\"{}\" params={} nextrun=\"{}\""
        s = s.format(
            cron.id,
            owner,
            cron.task_type,
            cron.schedule,
            str(cron.properties),
            cron.next.strftime("%c") if cron.next is not None else "null"
        )

        return s

    @commands.group(aliases=["schedule"], invoke_without_command=True)
    async def cron(self, ctx):
        """View or modify scheduled jobs."""
        await ctx.send("Specify a subcommand.")
        await ctx.send_help("cron")

    # Run a generic cron cmd.
    async def _run_croncmd(self, coro, ctx, cron_id, *args, require_ownership=False):
        crons = self.jc.sched_filter(guild_id=ctx.guild.id)

        if cron_id in crons:
            cron = crons[cron_id]

            cron_can_modify = (
                    ctx.author.guild_permissions.administrator or
                    cron.owner_id == ctx.member.id
            )

            if require_ownership and not cron_can_modify:
                raise NotAdministrator("force run schedules created by other users")

            try:
                # If a method is supplied, no need to feed in self
                if inspect.ismethod(coro):
                    await coro(ctx, cron, *args)
                else:
                    await coro(self, ctx, cron, *args)
            except job.ScheduleParseException as e:
                # Catch any schedule parse exceptions that might happen
                msg = "Could not parse cron str \"{}\": {}"
                await ctx.send(msg.format(e.cronstr, str(e)))
        else:
            await ctx.send("Schedule {} does not exist.".format(cron_id))

    # A command that operates on a single schedule entry by ID.
    # coro must be coro(self, ctx, cron)
    def _croncmd(entity=commands, name=None, **run_croncmd_args):
        def decorator(coro):
            nonlocal name

            if name is None:
                name = coro.__name__

            @util.command_wraps(coro, entity=entity, name=name)
            async def run(self, ctx, id: int):
                await self._run_croncmd(coro, ctx, id, **run_croncmd_args)

            return run

        return decorator

    @cron.command(name="list")
    async def cronlist(self, ctx):
        """List scheduled jobs."""
        crons = self.jc.sched_filter(guild_id=ctx.guild.id)
        cronlines = [self.pretty_print_cron(cron) for cron in crons.values()]

        if cronlines:
            await ctx.send(util.codelns(cronlines))
        else:
            await ctx.send("Nothing scheduled.")

    @cron.command(name="create")
    @commands.is_owner()
    async def croncreate(self, ctx, task_type: str, cronstr: str, *, params_json):
        """Create a schedule for an arbitrary job. Bot owner only."""
        if task_type not in self.registry:
            await ctx.send("Task type \"{}\" is not available.".format(
                task_type
            ))
            return

        params_json = params_json.strip()

        if params_json:
            params_dict = json.loads(params_json)
        else:
            params_dict = {}

        try:
            await self.core.schedule_job(ctx, task_type, params_dict, cronstr)
            await ack(ctx)

        except job.ScheduleParseException as e:
            msg = "Could not parse cron str \"{}\": {}"
            await ctx.send(msg.format(e.cronstr, str(e)))

    @_croncmd(cron, name="raw")
    async def cronraw(self, ctx, cron):
        """Print the internal representation of a schedule."""
        await ctx.send(util.codejson(cron.as_dict()))

    @_croncmd(cron, name="delete", require_ownership=True)
    async def crondelete(self, ctx, cron):
        """Delete a schedule."""
        await self.jc.delete_schedule(cron.id)
        await ack(ctx)

    @_croncmd(cron, name="force", require_ownership=True)
    async def cronforce(self, ctx, cron):
        """Force a scheduled job to run immediately."""
        await self.jc.run_now(cron.id)
        await ack(ctx)

    # TODO: Find a more compact way to express cron cmds that take extra
    # arguments
    async def _cronreschedule(self, ctx, cron, cronstr):
        await self.jc.reschedule(cron.id, cronstr)
        await ack(ctx)

    @cron.command(name="reschedule")
    async def cronreschedule(self, ctx, id: int, cronstr: str):
        """Reschedule a cron job."""
        await self._run_croncmd(
            self._cronreschedule, ctx, id, cronstr, require_ownership=True
        )

    @cron.command(name="flush")
    @commands.is_owner()
    async def cronflush(self, ctx):
        """Delete all schedules, and reset ID counter to 0. Bot owner only."""
        for id, cron in self.jc.sched_copy():
            await self.jc.delete_schedule(id)

        await self.core.config_db.get_common_config().set("last_schedule_id", 0)
        await ack(ctx)


# Optional cog for debugging core.job library functions
class JobDebug(commands.Cog):
    @commands.command()
    async def testcronparse(self, ctx, cron: str):
        """Test parsing of cron strings."""
        try:
            s_dict = job.cron_parse(cron)
        except job.ScheduleParseException as e:
            await ctx.send("Could not parse cron str: " + str(e))
            return

        await ctx.send(util.codejson(s_dict))

    @commands.command()
    async def testcronmatch(self, ctx, cron: str, date_time: str):
        """Test matching a cron string to a date.

        Date must be provided in ISO format:
        YYYY-MM-DDThh:mm:ss
        """
        try:
            if job.cron_match(cron, datetime.fromisoformat(date_time)):
                await ctx.send("Schedule match.")
            else:
                await ctx.send("No match.")

        except job.ScheduleParseException as e:
            await ctx.send("Could not parse cron str: " + str(e))
            return
        except ValueError as e:
            await ctx.send(str(e))
            return

    @commands.command()
    async def testcronnext(self, ctx, cron: str, date_time: typing.Optional[str]):
        """Test functionality to predict next run of a cron schedule.

        Date must be provided in ISO format:
        YYYY-MM-DDThh:mm:ss

        If date_time is not given, the current date will be used instead.
        """
        try:
            if date_time is None:
                date_time = datetime.now()
            else:
                date_time = datetime.fromisoformat(date_time)

            s = job.cron_parse(cron)
        except job.ScheduleParseException as e:
            await ctx.send("Could not parse cron str: " + str(e))
            return
        except ValueError as e:
            await ctx.send(str(e))
            return

        next_date_time = job.cron_next_date_as_datetime(s, date_time)

        message = util.codelns([
            "From {}",
            "{} will next run",
            "     {}"
        ])

        await ctx.send(message.format(
            date_time.strftime("%c"),
            cron,
            next_date_time.strftime("%c")
        ))


class ConfigCogBase(commands.Cog):
    def __init__(self, core):
        self.core = core

    # Decorator for creating a simple config command that sets/gets
    # one config option. For example:
    #
    # @ConfigCogBase.cfg_command()
    # async def config_option_name(*_):
    #     pass
    #
    # Would produce a command called "config-option-name", where
    #
    # "w!config-option-name" gets the config "config_option_name" and prints it.
    # "w!config-option-name value" sets the config to "value".
    #
    # The body of the function is called immediately before the value
    # is set, so it may be used for additional validation/processing.
    @staticmethod
    def cfg_command(
            converter=None,
            cfg_key=None,
            require_admin=True,
            **kwargs):

        def decorator(coro):
            if "name" not in kwargs:
                kwargs["name"] = coro.__name__.replace("_", "-")

            @util.command_wraps(coro, **kwargs)
            async def go(self, ctx, value: typing.Optional[str]):
                cfg = self.core.cfg(ctx)

                if cfg_key is None:
                    key = coro.__name__
                else:
                    key = cfg_key

                # GET
                if value is None:
                    display_name = key.replace("_", " ").capitalize()
                    value = cfg.get(key)
                    await ctx.reply("{} is {}.".format(display_name, value))
                    return

                # SET
                if require_admin and not ctx.author.guild_permissions.administrator:
                    await ctx.reply("You must be administrator to set this value.")
                    return

                if converter is not None:
                    value = converter(value)

                await coro(self, ctx, cfg, key, value)
                cfg.set(key, value)
                await ack(ctx)

            return go

        return decorator