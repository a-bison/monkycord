import discord


class NotAdministrator(discord.DiscordException):
    def __init__(self, task):
        self.task = task
