from discord.ext import commands

import json


def check_administrator():
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


# Workaround until I read the docs better
async def process_user_optional(ctx, member, rest):
    too_many_args = "Too many arguments. Try \"{}\"."
    nosuchuser = "Could not find user {}. Try exact case or a mention."

    if member and rest:
        correct = " ".join([member.name, *rest])
        await ctx.send(too_many_args.format(correct))
        return None

    if not member and rest:
        # the member conversion failed.
        if len(rest) > 1:
            await ctx.send(too_many_args.format(" ".join(rest)))
            return None

        await ctx.send(nosuchuser.format(rest[0]))
        return None

    if not member:
        return ctx.author

    return member


# React with a check mark for confirmation
async def ack(ctx):
    await ctx.message.add_reaction("\U00002705")


def code(s):
    # Note: Not using any format()s here, so we can construct format strings
    # using the util.code* funcs
    return "```\n" + s + "\n```"


def codelns(lns):
    return code("\n".join(lns))


def codejson(j):
    return code(json.dumps(j, indent=4))


# More primitive wrapper that does not set the __wrapped__ attribute.
# This forces the command decorator to use the wrapper function annotations
# for parameter checking, rather than the wrapped function's annotations.
#
# If specified, entity specifies what group this command should be added to.
def command_wraps(wrapped, entity=commands, *args, **kwargs):
    if entity is None:
        entity = commands

    def decorator(wrapper):
        wrapper.__name__ = wrapped.__name__
        wrapper.__qualname__ = wrapped.__qualname__
        wrapper.__doc__ = wrapped.__doc__
        wrapper.__module__ = wrapped.__module__

        return entity.command(*args, **kwargs)(wrapper)

    return decorator
