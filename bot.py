import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import datetime
import asyncio
from dateparser.search import search_dates
import re
from zoneinfo import ZoneInfo
import json
from dateutil.rrule import rrule, rrulestr, WEEKLY, MO, TU, WE, TH, FR, SA, SU
from dateutil.relativedelta import relativedelta
import spacy
import uuid

# --- Load Environment Variables ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# --- Bot Configuration ---
SERVER_TIMEZONE = "America/Chicago" # Default timezone if a user hasn't set their own.
TIMEZONES_FILE = 'user_timezones.json'

# --- In-memory Storage ---
meetings = []
meeting_exceptions = []  # Store individual instance modifications/cancellations
user_timezones = {}
nlp = None # To hold the spaCy model

# --- Trie for Spelling Correction ---
class TrieNode:
    def __init__(self):
        self.children = {}
        self.is_end_of_word = False
        self.word = None

class Trie:
    def __init__(self):
        self.root = TrieNode()

    def insert(self, word):
        node = self.root
        for char in word:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end_of_word = True
        node.word = word

    def search_best_match(self, word, max_distance=2):
        row = range(len(word) + 1)
        results = []

        def dfs(node, char, current_row):
            new_row = [current_row[0] + 1]
            for i, c in enumerate(word):
                cost = 0 if char == c else 1
                new_row.append(min(new_row[i] + 1, current_row[i] + cost, current_row[i+1] + 1))
            
            if new_row[-1] <= max_distance and node.is_end_of_word:
                results.append((node.word, new_row[-1]))

            if min(new_row) <= max_distance:
                for next_char, next_node in node.children.items():
                    dfs(next_node, next_char, new_row)
        
        for char, node in self.root.children.items():
            dfs(node, char, row)
            
        if word and results:
            first_letter = word[0].lower()
            # Sort by distance, then prioritize words starting with same letter
            results.sort(key=lambda x: (
                x[1],  # Primary: edit distance (lower is better)
                not x[0].startswith(first_letter),  # Secondary: same first letter (False sorts before True)
                x[0]   # Tertiary: alphabetical order
            ))
            return results[0] if results else (None, float('inf'))
        else:
            return min(results, key=lambda x: x[1]) if results else (None, float('inf'))
    
    def search_multiple_matches(self, word, max_distance=2):
        """Returns all matches within the distance threshold, sorted by distance"""
        row = range(len(word) + 1)
        results = []

        def dfs(node, char, current_row):
            new_row = [current_row[0] + 1]
            for i, c in enumerate(word):
                cost = 0 if char == c else 1
                new_row.append(min(new_row[i] + 1, current_row[i] + cost, current_row[i+1] + 1))
            
            if new_row[-1] <= max_distance and node.is_end_of_word:
                results.append((node.word, new_row[-1]))

            if min(new_row) <= max_distance:
                for next_char, next_node in node.children.items():
                    dfs(next_node, next_char, new_row)
        
        for char, node in self.root.children.items():
            dfs(node, char, row)
            
        # Sort by distance, then prioritize words starting with same letter, then alphabetically
        if word and results:
            first_letter = word[0].lower()
            results.sort(key=lambda x: (
                x[1],  # Primary: edit distance (lower is better)
                not x[0].startswith(first_letter),  # Secondary: same first letter (False sorts before True)
                x[0]   # Tertiary: alphabetical order
            ))
        else:
            results.sort(key=lambda x: (x[1], x[0]))
        return results

# Create and populate the Trie with correct spellings of common scheduling words
scheduling_trie = Trie()
scheduling_words = [
    # Days of the week
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    # Months
    "january", "february", "march", "april", "may", "june", 
    "july", "august", "september", "october", "november", "december",
    # Keywords
    "weekly", "every", "recurring",
    # Relative terms & Units
    "tomorrow", "today", "tonight", "next", "last",
    "hour", "hours", "minute", "minutes", "day", "days",
    "week", "weeks", "month", "months", "am", "pm"
]
for word in scheduling_words:
    scheduling_trie.insert(word)

# --- Helper functions for timezone storage ---
def save_user_timezones():
    """Saves the user timezones dictionary to a JSON file."""
    with open(TIMEZONES_FILE, 'w') as f:
        json.dump(user_timezones, f)

def load_user_timezones():
    """Loads user timezones from a JSON file when the bot starts."""
    global user_timezones
    try:
        with open(TIMEZONES_FILE, 'r') as f:
            user_timezones = json.load(f)
    except FileNotFoundError:
        user_timezones = {}

# --- Helper functions for meeting instances ---
def get_next_meeting_instances(meeting, from_time=None, limit=10):
    """Get the next instances of a recurring meeting, considering exceptions."""
    if meeting['type'] != 'recurring':
        return [meeting['time']] if meeting['type'] == 'one-time' else []
    
    if from_time is None:
        from_time = datetime.datetime.now(ZoneInfo("UTC"))
    
    rule = rrulestr(meeting['rule_string'])
    instances = []
    
    for instance_time in rule:
        if len(instances) >= limit:
            break
        if instance_time.replace(tzinfo=ZoneInfo("UTC")) >= from_time:
            # Check if this instance has been modified or cancelled
            exception = get_meeting_exception(meeting['id'], instance_time)
            if exception:
                if exception['type'] == 'cancelled':
                    continue  # Skip cancelled instances
                elif exception['type'] == 'rescheduled':
                    instances.append(exception['new_time'])
                    continue
            instances.append(instance_time.replace(tzinfo=ZoneInfo("UTC")))
    
    return instances

def get_meeting_exception(meeting_id, instance_time):
    """Get exception data for a specific meeting instance."""
    instance_key = f"{meeting_id}_{instance_time.isoformat()}"
    for exception in meeting_exceptions:
        if exception['instance_key'] == instance_key:
            return exception
    return None

def add_meeting_exception(meeting_id, original_time, exception_type, new_time=None):
    """Add an exception for a specific meeting instance."""
    instance_key = f"{meeting_id}_{original_time.isoformat()}"
    
    # Remove existing exception for this instance if it exists
    global meeting_exceptions
    meeting_exceptions = [e for e in meeting_exceptions if e['instance_key'] != instance_key]
    
    exception_data = {
        'instance_key': instance_key,
        'meeting_id': meeting_id,
        'original_time': original_time,
        'type': exception_type,  # 'cancelled' or 'rescheduled'
    }
    
    if exception_type == 'rescheduled' and new_time:
        exception_data['new_time'] = new_time
    
    meeting_exceptions.append(exception_data)

def get_meeting_display_info(meeting, user_tz):
    """Get display information for a meeting, considering exceptions."""
    if meeting['type'] == 'one-time':
        dt_utc = meeting['time'].replace(tzinfo=ZoneInfo("UTC"))
        dt_user_tz = dt_utc.astimezone(user_tz)
        return f"One-time: {dt_user_tz.strftime('%a, %b %d, %Y at %I:%M %p %Z')}"
    else:  # recurring
        dt_utc = meeting['dtstart'].replace(tzinfo=ZoneInfo("UTC"))
        dt_user_tz = dt_utc.astimezone(user_tz)
        
        # Check for exceptions in the next few instances
        instances = get_next_meeting_instances(meeting, limit=3)
        exception_count = len([e for e in meeting_exceptions if e['meeting_id'] == meeting['id']])
        
        base_label = f"Recurring: {dt_user_tz.strftime('%A')}s at {dt_user_tz.strftime('%I:%M %p %Z')}"
        if exception_count > 0:
            base_label += f" ({exception_count} exceptions)"
        
        return base_label

# --- UI Views for Interactive Components ---

class ConfirmationView(discord.ui.View):
    def __init__(self, author: discord.User, meeting_time: datetime.datetime, channel_id: int, recurrence_details: dict | None):
        super().__init__(timeout=180)
        self.author = author
        self.meeting_time = meeting_time
        self.channel_id = channel_id
        self.recurrence_details = recurrence_details

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        unix_timestamp = int(self.meeting_time.timestamp())
        content = ""
        meeting_id = str(uuid.uuid4())

        if self.recurrence_details:
            details = self.recurrence_details
            rule = None
            if details['type'] == 'specific_days':
                rule = rrule(freq=WEEKLY, dtstart=self.meeting_time, byweekday=details['days'])
            else: # Simple weekly
                rule = rrule(freq=WEEKLY, dtstart=self.meeting_time)

            if rule:
                meetings.append({
                    'id': meeting_id,
                    'type': 'recurring',
                    'rule_string': str(rule),
                    'dtstart': self.meeting_time,  # Store the start time explicitly
                    'requester': self.author.mention,
                    'channel_id': self.channel_id
                })
                content = f"✅ Confirmed: A **recurring** meeting is scheduled, starting <t:{unix_timestamp}:f>."
        
        if not content: # If it was a one-time meeting
            meetings.append({
                'id': meeting_id,
                'type': 'one-time',
                'time': self.meeting_time,
                'channel_id': self.channel_id,
                'requester': self.author.mention
            })
            content = f"✅ Confirmed: A meeting is scheduled for <t:{unix_timestamp}:f>."
        
        # Disable buttons and update the message
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Canceled.", view=self)
        self.stop()

class TimezoneSelectView(discord.ui.View):
    def __init__(self, author: discord.User):
        super().__init__(timeout=180)
        self.author = author
        common_timezones = [
            "UTC", "US/Pacific", "US/Mountain", "US/Central", "US/Eastern", "US/Hawaii", "US/Alaska",
            "Europe/London", "Europe/Berlin", "Europe/Moscow", "Europe/Paris", "Asia/Tokyo", 
            "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Australia/Sydney", "Australia/Perth", 
            "America/Toronto", "America/Sao_Paulo", "Africa/Cairo"
        ]
        select_options = [discord.SelectOption(label=tz) for tz in sorted(common_timezones)]
        self.select_menu = discord.ui.Select(placeholder="Select your timezone...", options=select_options)
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return
        selected_timezone = self.select_menu.values[0]
        user_timezones[str(interaction.user.id)] = selected_timezone
        save_user_timezones()
        self.select_menu.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"✅ Your timezone has been set to `{selected_timezone}`.", ephemeral=True)
        self.stop()


class MultiMeetingConfirmationView(discord.ui.View):
    def __init__(self, author: discord.User, scheduled_meetings: list, channel_id: int):
        super().__init__(timeout=180)
        self.author = author
        self.scheduled_meetings = scheduled_meetings
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm All", style=discord.ButtonStyle.success)
    async def confirm_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        created_meetings = []
        
        for meeting_data in self.scheduled_meetings:
            meeting_id = str(uuid.uuid4())
            
            if meeting_data['recurrence_details']:
                # Create recurring meeting
                if meeting_data['recurrence_details']['type'] == 'specific_days':
                    rule = rrule(freq=WEEKLY, dtstart=meeting_data['meeting_time_utc'], byweekday=meeting_data['recurrence_details']['days'])
                else:  # Simple weekly
                    rule = rrule(freq=WEEKLY, dtstart=meeting_data['meeting_time_utc'])
                
                meeting_entry = {
                    'id': meeting_id,
                    'type': 'recurring',
                    'rule_string': str(rule),
                    'dtstart': meeting_data['meeting_time_utc'],
                    'requester': self.author.mention,
                    'channel_id': self.channel_id
                }
                meetings.append(meeting_entry)
                created_meetings.append(f"✅ Recurring meeting: {meeting_data['corrected_text']}")
            else:
                # Create one-time meeting
                meeting_entry = {
                    'id': meeting_id,
                    'type': 'one-time',
                    'time': meeting_data['meeting_time_utc'],
                    'channel_id': self.channel_id,
                    'requester': self.author.mention
                }
                meetings.append(meeting_entry)
                created_meetings.append(f"✅ One-time meeting: {meeting_data['corrected_text']}")
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        
        success_message = f"**🎉 Successfully scheduled {len(created_meetings)} meeting(s)!**\n\n" + "\n".join(created_meetings)
        await interaction.response.edit_message(content=success_message, view=self, embed=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Canceled. No meetings were scheduled.", view=self, embed=None)
        self.stop()


class MeetingManagementView(discord.ui.View):
    def __init__(self, author: discord.User):
        super().__init__(timeout=300)
        self.author = author
        self.selected_meeting_id = None
        
        # The dropdown is added first. The buttons will be added by their decorators.
        self.meeting_select = self.create_select_menu()
        if self.meeting_select:
            self.add_item(self.meeting_select)
        
        # Add a second dropdown for individual instances if we have recurring meetings
        # Only add if we have room and there are recurring meetings
        if self.meeting_select and any(m['type'] == 'recurring' for m in meetings):
            self.instance_select = self.create_instance_select_menu()
            if self.instance_select:
                self.add_item(self.instance_select)
        else:
            self.instance_select = None

    def create_select_menu(self):
        """Builds the dropdown menu from the global meetings list."""
        if not meetings:
            return None

        # Get the user's timezone for display
        user_tz_str = user_timezones.get(str(self.author.id), SERVER_TIMEZONE)
        user_tz = ZoneInfo(user_tz_str)

        options = []
        for meeting in meetings:
            label = get_meeting_display_info(meeting, user_tz)
            options.append(discord.SelectOption(label=label, value=meeting['id']))
        
        select = discord.ui.Select(placeholder="Select a meeting to manage...", options=options, row=0)
        select.callback = self.on_meeting_select
        return select

    def create_instance_select_menu(self):
        """Builds a dropdown menu for individual meeting instances (limited to fit Discord's constraints)."""
        # Get user's timezone for display
        user_tz_str = user_timezones.get(str(self.author.id), SERVER_TIMEZONE)
        user_tz = ZoneInfo(user_tz_str)
        
        # Collect all instances with their times for sorting
        all_instances = []
        
        for meeting in meetings:
            if meeting['type'] == 'recurring':
                instances = get_next_meeting_instances(meeting, limit=3)  # Limit to 3 instances per meeting
                for instance_time in instances:
                    # Get the actual time (considering rescheduling)
                    actual_time = instance_time
                    exception = get_meeting_exception(meeting['id'], instance_time)
                    if exception and exception['type'] == 'rescheduled':
                        actual_time = exception['new_time']
                    
                    all_instances.append({
                        'meeting_id': meeting['id'],
                        'original_time': instance_time,
                        'actual_time': actual_time,
                        'exception': exception
                    })
        
        # Sort all instances by actual time (closest first)
        all_instances.sort(key=lambda x: x['actual_time'])
        
        # Build options from sorted instances
        options = []
        for i, instance_data in enumerate(all_instances):
            # Stop if we're approaching Discord's limit of 25 options per dropdown
            if len(options) >= 20:
                break
            
            instance_time = instance_data['original_time']
            actual_time = instance_data['actual_time']
            exception = instance_data['exception']
            
            display_time = actual_time.astimezone(user_tz)
            
            # Calculate days from now for better labeling
            now = datetime.datetime.now(ZoneInfo("UTC"))
            days_diff = (actual_time.date() - now.date()).days
            
            if days_diff == 0:
                day_label = "Today"
            elif days_diff == 1:
                day_label = "Tomorrow"
            elif days_diff <= 7:
                day_label = display_time.strftime('%a')  # Mon, Tue, etc.
            else:
                day_label = display_time.strftime('%a, %b %d')  # Mon, Jul 21
            
            label = f"{day_label} at {display_time.strftime('%I:%M %p')}"
            
            # Add modification indicators
            if exception:
                if exception['type'] == 'rescheduled':
                    original_display = instance_time.astimezone(user_tz)
                    label += f" (moved from {original_display.strftime('%I:%M %p')})"
                elif exception['type'] == 'cancelled':
                    label += " (CANCELLED)"
            
            # Truncate label if too long (Discord has a 100 char limit)
            if len(label) > 90:
                label = label[:87] + "..."
            
            # Create a unique value for this specific instance
            instance_value = f"{instance_data['meeting_id']}|{instance_time.isoformat()}"
            options.append(discord.SelectOption(label=label, value=instance_value))
        
        if not options:
            return None
        
        select = discord.ui.Select(placeholder="Select a specific upcoming instance (sorted by time)...", options=options, row=1)
        select.callback = self.on_instance_select
        return select

    async def on_instance_select(self, interaction: discord.Interaction):
        """Callback for when a user selects a specific meeting instance."""
        await interaction.response.defer()
        
        # Parse the selected value
        meeting_id, instance_time_str = self.instance_select.values[0].split('|')
        instance_time = datetime.datetime.fromisoformat(instance_time_str)
        
        # Show options for this specific instance
        view = InstanceManagementView(
            author=self.author,
            meeting_id=meeting_id,
            instance_time=instance_time,
            original_view_message=interaction.message
        )
        
        user_tz_str = user_timezones.get(str(self.author.id), SERVER_TIMEZONE)
        user_tz = ZoneInfo(user_tz_str)
        display_time = instance_time.astimezone(user_tz)
        
        await interaction.message.edit(
            content=f"Managing specific instance: **{display_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}**",
            view=view
        )
        self.stop()

    async def on_meeting_select(self, interaction: discord.Interaction):
        """Callback for when a user selects a meeting from the dropdown."""
        await interaction.response.defer()
        
        self.selected_meeting_id = self.meeting_select.values[0]
        
        # The buttons are guaranteed to exist on `self` now.
        self.cancel_button.disabled = False
        self.reschedule_button.disabled = False
        
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Cancel Meeting", style=discord.ButtonStyle.danger, disabled=True, row=2)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global meetings
        if not self.selected_meeting_id:
            await interaction.response.send_message("Please select a meeting first.", ephemeral=True)
            return

        # Find and remove the meeting
        original_meeting_count = len(meetings)
        meetings = [m for m in meetings if m['id'] != self.selected_meeting_id]
        
        if len(meetings) < original_meeting_count:
            # Refresh the view by creating a new one
            new_view = MeetingManagementView(author=self.author)
            await interaction.response.edit_message(
                content="✅ Meeting canceled. Select another meeting to manage or dismiss this message.", 
                view=new_view
            )
            self.stop()
        else:
            await interaction.response.send_message("Error: Could not find the meeting to cancel.", ephemeral=True)

    @discord.ui.button(label="Reschedule", style=discord.ButtonStyle.primary, disabled=True, row=2)
    async def reschedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_meeting_id:
            await interaction.response.send_message("Please select a meeting first.", ephemeral=True)
            return
        
        # Find the selected meeting
        meeting = next((m for m in meetings if m['id'] == self.selected_meeting_id), None)
        if not meeting:
            await interaction.response.send_message("Error: Could not find the meeting.", ephemeral=True)
            return
        
        if meeting['type'] == 'recurring':
            # For recurring meetings, show choice between instance vs series
            view = RescheduleChoiceView(
                meeting_id=self.selected_meeting_id,
                original_view_message=interaction.message
            )
            await interaction.response.send_message(
                "This is a recurring meeting. What would you like to reschedule?", 
                view=view, 
                ephemeral=True
            )
        else:
            # For one-time meetings, go directly to reschedule modal
            modal = RescheduleModal(
                meeting_id=self.selected_meeting_id, 
                original_view_message=interaction.message,
                reschedule_type="one-time"
            )
            await interaction.response.send_modal(modal)
        
        self.stop()

    @discord.ui.button(label="Clear All Meetings", style=discord.ButtonStyle.danger, row=3)
    async def clear_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show confirmation dialog
        view = ClearAllConfirmationView(author=self.author, original_view_message=interaction.message)
        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to delete ALL meetings?**\n\n"
            f"This will permanently delete:\n"
            f"• **{len([m for m in meetings if m['type'] == 'one-time'])}** one-time meetings\n"
            f"• **{len([m for m in meetings if m['type'] == 'recurring'])}** recurring meeting series\n"
            f"• **{len(meeting_exceptions)}** individual instance modifications\n\n"
            f"**This action cannot be undone!**",
            view=view,
            ephemeral=True
        )


class ClearAllConfirmationView(discord.ui.View):
    def __init__(self, author: discord.User, original_view_message: discord.Message):
        super().__init__(timeout=180)
        self.author = author
        self.original_view_message = original_view_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, Delete Everything", style=discord.ButtonStyle.danger)
    async def confirm_clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        global meetings, meeting_exceptions
        
        # Count what we're deleting for the confirmation message
        one_time_count = len([m for m in meetings if m['type'] == 'one-time'])
        recurring_count = len([m for m in meetings if m['type'] == 'recurring'])
        exception_count = len(meeting_exceptions)
        
        # Clear everything
        meetings.clear()
        meeting_exceptions.clear()
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        
        # Update the original view to show it's empty
        await self.original_view_message.edit(
            content="🗑️ All meetings have been cleared.",
            view=None
        )
        
        await interaction.response.edit_message(
            content=f"✅ **All meetings cleared!**\n\n"
                   f"Deleted:\n"
                   f"• **{one_time_count}** one-time meetings\n"
                   f"• **{recurring_count}** recurring meeting series\n"
                   f"• **{exception_count}** individual modifications\n\n"
                   f"Your meeting schedule is now empty.",
            view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ **Cancelled** - No meetings were deleted.",
            view=self
        )
        self.stop()


class InstanceManagementView(discord.ui.View):
    def __init__(self, author: discord.User, meeting_id: str, instance_time: datetime.datetime, original_view_message: discord.Message):
        super().__init__(timeout=300)
        self.author = author
        self.meeting_id = meeting_id
        self.instance_time = instance_time
        self.original_view_message = original_view_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cancel This Instance", style=discord.ButtonStyle.danger)
    async def cancel_instance(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Add an exception to cancel this specific instance
        add_meeting_exception(
            meeting_id=self.meeting_id,
            original_time=self.instance_time,
            exception_type='cancelled'
        )
        
        user_tz_str = user_timezones.get(str(self.author.id), SERVER_TIMEZONE)
        user_tz = ZoneInfo(user_tz_str)
        display_time = self.instance_time.astimezone(user_tz)
        
        # Refresh the original view
        refreshed_view = MeetingManagementView(author=self.author)
        await self.original_view_message.edit(
            content="✅ Instance cancelled. Select another meeting to manage.", 
            view=refreshed_view
        )
        await interaction.response.send_message(
            f"✅ **Instance cancelled:** {display_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
        )
        self.stop()

    @discord.ui.button(label="Reschedule This Instance", style=discord.ButtonStyle.primary)
    async def reschedule_instance(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RescheduleModal(
            meeting_id=self.meeting_id,
            original_view_message=self.original_view_message,
            reschedule_type="specific-instance",
            instance_time=self.instance_time
        )
        await interaction.response.send_modal(modal)
        self.stop()

    @discord.ui.button(label="Back to Meeting List", style=discord.ButtonStyle.secondary)
    async def back_to_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        refreshed_view = MeetingManagementView(author=self.author)
        await interaction.response.edit_message(
            content="Select a meeting from the dropdown below to manage it.",
            view=refreshed_view
        )
        self.stop()


class RescheduleChoiceView(discord.ui.View):
    def __init__(self, meeting_id: str, original_view_message: discord.Message):
        super().__init__(timeout=180)
        self.meeting_id = meeting_id
        self.original_view_message = original_view_message

    @discord.ui.button(label="Just Next Instance", style=discord.ButtonStyle.secondary)
    async def reschedule_instance(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RescheduleModal(
            meeting_id=self.meeting_id,
            original_view_message=self.original_view_message,
            reschedule_type="next-instance"
        )
        await interaction.response.send_modal(modal)
        self.stop()

    @discord.ui.button(label="All Future Meetings", style=discord.ButtonStyle.primary)
    async def reschedule_series(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RescheduleModal(
            meeting_id=self.meeting_id,
            original_view_message=self.original_view_message,
            reschedule_type="all-future"
        )
        await interaction.response.send_modal(modal)
        self.stop()


class RescheduleModal(discord.ui.Modal, title="Reschedule Meeting"):
    def __init__(self, meeting_id: str, original_view_message: discord.Message, reschedule_type: str, instance_time: datetime.datetime = None):
        super().__init__()
        self.meeting_id = meeting_id
        self.original_view_message = original_view_message
        self.reschedule_type = reschedule_type  # "one-time", "next-instance", "all-future", "specific-instance"
        self.instance_time = instance_time  # For specific-instance reschedules
    
    new_time = discord.ui.TextInput(
        label="New Date and Time",
        placeholder="e.g., tomorrow at 5pm, next Friday at 3pm, July 25 at 2:30pm",
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        global meetings
        # Defer the response so we can use followup messages
        await interaction.response.defer(ephemeral=True)
        
        # Use the same parsing logic as the schedule command
        author_id = str(interaction.user.id)
        active_timezone = user_timezones.get(author_id, SERVER_TIMEZONE)

        # Apply spell-checking to the input
        words = self.new_time.value.lower().split()
        corrected_words = []
        for word in words:
            # Very permissive distance calculation based on word length
            if len(word) <= 3:
                adaptive_max_distance = 0  # Short words must be exact
            elif len(word) == 4:
                adaptive_max_distance = 2  # 4-char words allow 2 errors
            elif len(word) <= 6:
                adaptive_max_distance = 3  # 5-6 char words allow 3 errors
            else:
                adaptive_max_distance = 5  # 7+ char words allow 5 errors
            
            best_match, distance = scheduling_trie.search_best_match(word, max_distance=adaptive_max_distance)
            if best_match:
                corrected_words.append(best_match)
            else:
                corrected_words.append(word)
        corrected_text = " ".join(corrected_words)

        # Use the same weekday parsing logic as the main schedule command
        weekday_map = {"monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH, "friday": FR, "saturday": SA, "sunday": SU}
        found_day_consts = set()
        for day_str, day_const in weekday_map.items():
            if re.search(r'\b' + day_str + r'\b', corrected_text):
                found_day_consts.add(day_const)
        found_weekdays = list(found_day_consts)

        # Determine if this should be recurring (same logic as main schedule command)
        is_recurring = "every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text or len(found_weekdays) > 1
        is_single_day_recurrence = (
            ("every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text) 
            and len(found_weekdays) == 1
        )

        recurrence_details = None
        if is_recurring or is_single_day_recurrence:
            if found_weekdays:
                recurrence_details = {'type': 'specific_days', 'days': found_weekdays}
            else:
                recurrence_details = {'type': 'simple_weekly'}

        current_time = datetime.datetime.now(ZoneInfo(active_timezone))

        found_dates = search_dates(
            corrected_text,
            settings={
                'TIMEZONE': active_timezone,
                'TO_TIMEZONE': active_timezone,  # Keep it in user timezone first
                'RETURN_AS_TIMEZONE_AWARE': True,
                'RELATIVE_BASE': current_time
            }
        )
        
        if not found_dates:
            await interaction.followup.send("I couldn't understand the new time. Please try something like 'tomorrow at 3pm' or 'in 2 hours'.", ephemeral=True)
            return
        
        _, new_meeting_local = found_dates[0]

        # Convert from user timezone to UTC for storage
        if new_meeting_local.tzinfo is None:
            # If dateparser didn't return timezone info, assume user timezone
            new_meeting_local = new_meeting_local.replace(tzinfo=ZoneInfo(active_timezone))
        
        new_meeting_utc = new_meeting_local.astimezone(ZoneInfo("UTC"))



        # If the parsed date is in the past, advance to next occurrence instead of next year
        now_utc = datetime.datetime.now(ZoneInfo("UTC"))
        if new_meeting_utc < now_utc:
            # Instead of advancing by a year, advance by weeks until we get a future date
            while new_meeting_utc < now_utc:
                new_meeting_utc += relativedelta(weeks=1)

        # Find the meeting to update
        meeting_to_update = next((m for m in meetings if m['id'] == self.meeting_id), None)
        if not meeting_to_update:
            await interaction.followup.send("Error: Could not find the meeting to reschedule.", ephemeral=True)
            return

        # Convert the new time to user's timezone for display
        user_tz = ZoneInfo(active_timezone)
        new_meeting_display = new_meeting_utc.astimezone(user_tz)
        
        success_message = ""
        
        if self.reschedule_type == "next-instance":
            # Reschedule just the next instance of a recurring meeting
            if meeting_to_update['type'] != 'recurring':
                await interaction.followup.send("Error: This meeting is not recurring.", ephemeral=True)
                return
            
            # Get the next instance time
            next_instances = get_next_meeting_instances(meeting_to_update, limit=1)
            if not next_instances:
                await interaction.followup.send("No upcoming instances found for this meeting.", ephemeral=True)
                return
            
            original_instance_time = next_instances[0]
            
            # Add an exception for this specific instance
            add_meeting_exception(
                meeting_id=self.meeting_id,
                original_time=original_instance_time,
                exception_type='rescheduled',
                new_time=new_meeting_utc
            )
            
            # Create display strings
            original_display = original_instance_time.astimezone(user_tz)
            new_time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
            original_time_str = original_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
            
            success_message = f"✅ **Next instance only** rescheduled from **{original_time_str}** to **{new_time_str}**"
        
        elif self.reschedule_type == "specific-instance":
            # Reschedule a specific instance that was selected from the instance dropdown
            if not self.instance_time:
                await interaction.followup.send("Error: No specific instance time provided.", ephemeral=True)
                return
            
            # Add an exception for this specific instance
            add_meeting_exception(
                meeting_id=self.meeting_id,
                original_time=self.instance_time,
                exception_type='rescheduled',
                new_time=new_meeting_utc
            )
            
            # Create display strings
            original_display = self.instance_time.astimezone(user_tz)
            new_time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
            original_time_str = original_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
            
            success_message = f"✅ **Specific instance** rescheduled from **{original_time_str}** to **{new_time_str}**"
        
        elif self.reschedule_type == "all-future":
            # Reschedule the entire recurring series (original behavior)
            if recurrence_details:
                # Set up as recurring meeting
                if recurrence_details['type'] == 'specific_days':
                    rule = rrule(freq=WEEKLY, dtstart=new_meeting_utc, byweekday=recurrence_details['days'])
                else:  # Simple weekly
                    rule = rrule(freq=WEEKLY, dtstart=new_meeting_utc)
                
                meeting_to_update['type'] = 'recurring'
                meeting_to_update['rule_string'] = str(rule)
                meeting_to_update['dtstart'] = new_meeting_utc
                if 'time' in meeting_to_update:
                    del meeting_to_update['time']
                
                if recurrence_details['type'] == 'specific_days':
                    # Create a readable list of days
                    day_names = {v: k.capitalize() for k, v in weekday_map.items()}
                    day_list_str = ", ".join([day_names[day] for day in sorted(recurrence_details['days'])])
                    time_str = new_meeting_display.strftime('%I:%M %p %Z')
                    date_str = new_meeting_display.strftime('%B %d, %Y')
                    success_message = f"✅ **All future meetings** rescheduled to **every {day_list_str}** at **{time_str}**. First meeting: **{date_str}**"
                else:
                    time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                    success_message = f"✅ **All future meetings** rescheduled to **{time_str}** (recurring weekly)"
            else:
                # Convert recurring to one-time
                meeting_to_update['type'] = 'one-time'
                meeting_to_update['time'] = new_meeting_utc
                meeting_to_update['dtstart'] = new_meeting_utc
                if 'rule_string' in meeting_to_update:
                    del meeting_to_update['rule_string']
                
                new_time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                success_message = f"✅ **Recurring series converted to one-time meeting** for **{new_time_str}**"
        
        else:  # self.reschedule_type == "one-time"
            # Update one-time meeting (original behavior for one-time meetings)
            if recurrence_details:
                # Convert one-time to recurring
                if recurrence_details['type'] == 'specific_days':
                    rule = rrule(freq=WEEKLY, dtstart=new_meeting_utc, byweekday=recurrence_details['days'])
                else:  # Simple weekly
                    rule = rrule(freq=WEEKLY, dtstart=new_meeting_utc)
                
                meeting_to_update['type'] = 'recurring'
                meeting_to_update['rule_string'] = str(rule)
                meeting_to_update['dtstart'] = new_meeting_utc
                if 'time' in meeting_to_update:
                    del meeting_to_update['time']
                
                if recurrence_details['type'] == 'specific_days':
                    day_names = {v: k.capitalize() for k, v in weekday_map.items()}
                    day_list_str = ", ".join([day_names[day] for day in sorted(recurrence_details['days'])])
                    time_str = new_meeting_display.strftime('%I:%M %p %Z')
                    date_str = new_meeting_display.strftime('%B %d, %Y')
                    success_message = f"✅ **One-time meeting converted to recurring** - every **{day_list_str}** at **{time_str}**. First meeting: **{date_str}**"
                else:
                    time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                    success_message = f"✅ **One-time meeting converted to recurring** - **{time_str}** (weekly)"
            else:
                # Keep as one-time meeting
                meeting_to_update['type'] = 'one-time'
                meeting_to_update['time'] = new_meeting_utc
                meeting_to_update['dtstart'] = new_meeting_utc
                if 'rule_string' in meeting_to_update:
                    del meeting_to_update['rule_string']
                
                new_time_str = new_meeting_display.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                success_message = f"✅ **Meeting rescheduled** to **{new_time_str}**"

        # Refresh the original view
        refreshed_view = MeetingManagementView(author=interaction.user)
        await self.original_view_message.edit(
            content="✅ Meeting rescheduled. Select another meeting to manage.", 
            view=refreshed_view
        )
        await interaction.followup.send(success_message)
        

# --- Bot Setup ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# --- Bot Commands ---

@bot.command(name='help', help='Shows this help message.')
async def help(ctx):
    """Displays a help message for all commands."""
    embed = discord.Embed(
        title="🤖 Meeting Time Handler - Commands",
        description="Available commands for the Discord meeting bot:",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="`!schedule` or `!sched`",
        value="Schedule meetings using natural language",
        inline=False
    )
    
    embed.add_field(
        name="`!meetings` or `!meeting`",
        value="View and manage your scheduled meetings (including clear all option)",
        inline=False
    )
    
    embed.add_field(
        name="`!setzone`",
        value="Set your personal timezone",
        inline=False
    )
    
    embed.add_field(
        name="`!timecheck`",
        value="Check current time and timezone settings",
        inline=False
    )
    
    await ctx.send(embed=embed)


@bot.command(name='setzone', help='Set your personal timezone using an interactive menu.')
async def setzone(ctx):
    """Allows a user to save their personal timezone via a dropdown menu."""
    view = TimezoneSelectView(author=ctx.author)
    await ctx.send(
        "Please select your timezone from the list below. This helps me understand times like 'tomorrow at 6pm' correctly for you.", 
        view=view
    )

async def handle_multiple_meetings(ctx, time_text: str, author_id: str, active_timezone: str):
    """Handle scheduling multiple meetings from a single command."""
    # Clean up the input text
    clean_text = time_text.lower().strip()
    
    # Check if this is a single recurring pattern with multiple days (e.g., "every saturday and sunday at 7pm")
    weekday_map = {"monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH, "friday": FR, "saturday": SA, "sunday": SU}
    
    # Look for patterns like "every [day] and [day] at [time]" or "every [day], [day] at [time]"
    import re
    
    # Check if the text contains multiple days with a single time
    found_days = []
    for day_str in weekday_map.keys():
        if re.search(r'\b' + day_str + r'\b', clean_text):
            found_days.append(day_str)
    
    # If we found multiple days and there's a recurring keyword, create separate meetings for each day
    if len(found_days) > 1 and ("every" in clean_text or "weekly" in clean_text or "recurring" in clean_text):
        print(f"[DEBUG] Detected multi-day recurring pattern: {clean_text}")
        
        # Remove prefixes from the multi-day pattern
        prefixes_to_remove = ["make a meeting for", "schedule a meeting for", "make meetings for", "schedule meetings for", "make a meeting", "schedule a meeting"]
        processed_text = clean_text
        for prefix in prefixes_to_remove:
            if processed_text.startswith(prefix):
                processed_text = processed_text[len(prefix):].strip()
                break
        
        print(f"[DEBUG] After prefix removal: {processed_text}")
        
        # Create separate patterns for each day
        meeting_patterns = []
        for day in found_days:
            # Replace the multi-day pattern with single day pattern
            single_day_pattern = processed_text
            for other_day in found_days:
                if other_day != day:
                    # Remove other days from the pattern
                    single_day_pattern = single_day_pattern.replace(f" and {other_day}", "")
                    single_day_pattern = single_day_pattern.replace(f"{other_day} and ", "")
                    single_day_pattern = single_day_pattern.replace(f", {other_day}", "")
                    single_day_pattern = single_day_pattern.replace(f"{other_day}, ", "")
                    single_day_pattern = single_day_pattern.replace(other_day, "")
            
            # Clean up any double spaces and trim
            single_day_pattern = " ".join(single_day_pattern.split())
            meeting_patterns.append(single_day_pattern)
        
        print(f"[DEBUG] Created separate patterns: {meeting_patterns}")
        
        # Process each pattern separately
        scheduled_meetings = []
        failed_meetings = []
        
        for i, pattern in enumerate(meeting_patterns):
            try:
                result = await process_single_meeting_pattern(pattern, str(ctx.author.id), active_timezone, ctx)
                if result:
                    scheduled_meetings.append(result)
                else:
                    failed_meetings.append(f"Pattern {i+1}: '{pattern}'")
            except Exception as e:
                failed_meetings.append(f"Pattern {i+1}: '{pattern}' (Error: {str(e)})")
        
        # Show comprehensive results
        await show_multiple_meetings_confirmation(ctx, scheduled_meetings, failed_meetings)
        return
    
    # Split by "and" and commas, handling both
    # Split by comma or " and " while preserving the content
    parts = re.split(r',\s*(?:and\s+)?|(?:\s+and\s+)', clean_text)
    
    # Clean up each part and remove prefixes from each individual pattern
    meeting_patterns = []
    prefixes_to_remove = ["make a meeting for", "schedule a meeting for", "make meetings for", "schedule meetings for", "make a meeting", "schedule a meeting"]
    
    for part in parts:
        part = part.strip()
        if part:  # Skip empty parts
            # Remove prefixes from each individual pattern
            for prefix in prefixes_to_remove:
                if part.startswith(prefix):
                    part = part[len(prefix):].strip()
                    break
            meeting_patterns.append(part)

    print(f"[DEBUG] Meeting patterns to process: {meeting_patterns}")
    
    if not meeting_patterns:
        await ctx.send("I couldn't find any meeting patterns to schedule.")
        return
    
    # Process each meeting pattern
    scheduled_meetings = []
    failed_meetings = []
    
    for i, pattern in enumerate(meeting_patterns):
        try:
            result = await process_single_meeting_pattern(pattern, author_id, active_timezone, ctx)
            if result:
                scheduled_meetings.append(result)
            else:
                failed_meetings.append(f"Pattern {i+1}: '{pattern}'")
        except Exception as e:
            failed_meetings.append(f"Pattern {i+1}: '{pattern}' (Error: {str(e)})")
    
    # Show comprehensive results
    await show_multiple_meetings_confirmation(ctx, scheduled_meetings, failed_meetings)

async def process_single_meeting_pattern(pattern: str, author_id: str, active_timezone: str, ctx):
    """Process a single meeting pattern and return meeting details."""
    # Apply spell checking to the pattern
    words = pattern.lower().split()
    corrected_words = []
    for word in words:
        # Same spell checking logic as main schedule command
        if len(word) <= 3:
            adaptive_max_distance = 0
        elif len(word) == 4:
            adaptive_max_distance = 2
        elif len(word) <= 6:
            adaptive_max_distance = 3
        else:
            adaptive_max_distance = 5
        
        best_match, _ = scheduling_trie.search_best_match(word, max_distance=adaptive_max_distance)
        if best_match:
            corrected_words.append(best_match)
        else:
            corrected_words.append(word)
    
    corrected_text = " ".join(corrected_words)
    
    # Parse weekdays and recurrence (same logic as main function)
    weekday_map = {"monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH, "friday": FR, "saturday": SA, "sunday": SU}
    found_day_consts = set()
    for day_str, day_const in weekday_map.items():
        if re.search(r'\b' + day_str + r'\b', corrected_text):
            found_day_consts.add(day_const)
    found_weekdays = list(found_day_consts)

    # Determine if this should be recurring
    is_recurring = "every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text or len(found_weekdays) > 1
    is_single_day_recurrence = (
        ("every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text) 
        and len(found_weekdays) == 1
    )

    recurrence_details = None
    if is_recurring or is_single_day_recurrence:
        if found_weekdays:
            recurrence_details = {'type': 'specific_days', 'days': found_weekdays}
        else:
            recurrence_details = {'type': 'simple_weekly'}

    # Parse the date/time
    current_time = datetime.datetime.now(ZoneInfo(active_timezone))
    
    # For multi-day patterns, try to parse with just one day to get the time
    parse_text = corrected_text
    if len(found_weekdays) > 1:
        # Replace "day1 and day2" with just "day1" for time parsing
        for day_str in weekday_map.keys():
            if day_str in corrected_text:
                # Use the first day found for time parsing
                parse_text = corrected_text.replace(" and ", " ").replace(", ", " ")
                # Remove all days except the first one
                for other_day in weekday_map.keys():
                    if other_day != day_str and other_day in parse_text:
                        parse_text = parse_text.replace(other_day, "")
                parse_text = parse_text.replace("  ", " ").strip()
                break
    
    print(f"[DEBUG] Pattern '{pattern}' -> Attempting to parse: '{parse_text}'")
    print(f"[DEBUG] Pattern '{pattern}' -> Current time: {current_time}")
    
    found_dates = search_dates(
        parse_text,
        settings={
            'TIMEZONE': active_timezone,
            'TO_TIMEZONE': active_timezone,
            'RETURN_AS_TIMEZONE_AWARE': True,
            'RELATIVE_BASE': current_time
        }
    )
    
    print(f"[DEBUG] Pattern '{pattern}' -> search_dates result: {found_dates}")
    
    if not found_dates:
        print(f"[DEBUG] Pattern '{pattern}' -> Failed to parse time")
        return None  # Failed to parse

    _, meeting_time_local = found_dates[0]
    print(f"[DEBUG] Pattern '{pattern}' -> Parsed local time: {meeting_time_local}")

    # Convert from user timezone to UTC for storage
    if meeting_time_local.tzinfo is None:
        meeting_time_local = meeting_time_local.replace(tzinfo=ZoneInfo(active_timezone))
    
    meeting_time_utc = meeting_time_local.astimezone(ZoneInfo("UTC"))
    print(f"[DEBUG] Pattern '{pattern}' -> UTC time: {meeting_time_utc}")

    # For recurring meetings on specific days, we need to handle the time advancement more carefully
    now_utc = datetime.datetime.now(ZoneInfo("UTC"))
    print(f"[DEBUG] Pattern '{pattern}' -> Current UTC: {now_utc}")
    
    if meeting_time_utc < now_utc:
        print(f"[DEBUG] Pattern '{pattern}' -> Time is in the past, advancing...")
        if recurrence_details and recurrence_details['type'] == 'specific_days':
            # For recurring meetings, find the next occurrence of the specified day at the same time
            # Get the time components from the original LOCAL time, not UTC
            target_hour = meeting_time_local.hour
            target_minute = meeting_time_local.minute
            target_second = meeting_time_local.second
            print(f"[DEBUG] Pattern '{pattern}' -> Target time components: {target_hour}:{target_minute}:{target_second}")
            
            # Calculate the next occurrence in the user's timezone first, then convert to UTC
            now_local = now_utc.astimezone(ZoneInfo(active_timezone))
            
            # Find the next occurrence of any of the specified days
            for day_const in recurrence_details['days']:
                # Calculate days until next occurrence of this day
                current_weekday = now_local.weekday()
                target_weekday = day_const.weekday
                days_ahead = (target_weekday - current_weekday) % 7
                if days_ahead == 0:
                    # If it's the same day, check if the time has passed
                    today_at_target_time = now_local.replace(hour=target_hour, minute=target_minute, second=target_second, microsecond=0)
                    if today_at_target_time <= now_local:
                        days_ahead = 7  # If it's the same day but time has passed, go to next week
                
                print(f"[DEBUG] Pattern '{pattern}' -> Current weekday: {current_weekday}, Target weekday: {target_weekday}, Days ahead: {days_ahead}")
                
                # Calculate the next occurrence in local time
                next_occurrence_local = now_local + relativedelta(days=days_ahead, hour=target_hour, minute=target_minute, second=target_second, microsecond=0)
                print(f"[DEBUG] Pattern '{pattern}' -> Next occurrence (local): {next_occurrence_local}")
                
                # Convert to UTC
                next_occurrence_utc = next_occurrence_local.astimezone(ZoneInfo("UTC"))
                print(f"[DEBUG] Pattern '{pattern}' -> Next occurrence (UTC): {next_occurrence_utc}")
                
                if next_occurrence_utc > now_utc:
                    meeting_time_utc = next_occurrence_utc
                    print(f"[DEBUG] Pattern '{pattern}' -> Using next occurrence: {meeting_time_utc}")
                    break
            else:
                # If no valid next occurrence found, advance by a week
                meeting_time_utc += relativedelta(weeks=1)
                print(f"[DEBUG] Pattern '{pattern}' -> No valid occurrence found, advancing by week: {meeting_time_utc}")
        else:
            # For non-recurring or simple weekly meetings, use the original logic
            while meeting_time_utc < now_utc:
                meeting_time_utc += relativedelta(weeks=1)
            print(f"[DEBUG] Pattern '{pattern}' -> Advanced by weeks: {meeting_time_utc}")
    else:
        print(f"[DEBUG] Pattern '{pattern}' -> Time is in the future, no advancement needed")

    print(f"[DEBUG] Pattern '{pattern}' -> Final UTC time: {meeting_time_utc}")
    final_local = meeting_time_utc.astimezone(ZoneInfo(active_timezone))
    print(f"[DEBUG] Pattern '{pattern}' -> Final local time: {final_local}")

    return {
        'original_pattern': pattern,
        'corrected_text': corrected_text,
        'meeting_time_utc': meeting_time_utc,
        'recurrence_details': recurrence_details,
        'weekdays': found_weekdays
    }

async def show_multiple_meetings_confirmation(ctx, scheduled_meetings: list, failed_meetings: list):
    """Show confirmation for multiple meetings and create them if approved."""
    if not scheduled_meetings and not failed_meetings:
        await ctx.send("No meetings could be processed.")
        return
    
    embed = discord.Embed(
        title="🗓️ Multiple Meetings Found",
        description=f"I found {len(scheduled_meetings)} meeting pattern(s) to schedule:",
        color=discord.Color.blue()
    )
    
    # Show successful meetings
    if scheduled_meetings:
        active_timezone = user_timezones.get(str(ctx.author.id), SERVER_TIMEZONE)
        display_tz = ZoneInfo(active_timezone)
        
        meeting_descriptions = []
        weekday_map = {"monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH, "friday": FR, "saturday": SA, "sunday": SU}
        
        for i, meeting in enumerate(scheduled_meetings):
            display_dt = meeting['meeting_time_utc'].astimezone(display_tz)
            
            if meeting['recurrence_details']:
                if meeting['recurrence_details']['type'] == 'specific_days':
                    day_names = {v: k.capitalize() for k, v in weekday_map.items()}
                    # Sort by weekday value (0=Monday, 1=Tuesday, etc.)
                    sorted_days = sorted(meeting['recurrence_details']['days'], key=lambda x: x.weekday)
                    day_list = [day_names[day] for day in sorted_days]
                    day_str = ", ".join(day_list)
                    time_str = display_dt.strftime('%I:%M %p %Z')
                    meeting_descriptions.append(f"**{i+1}.** Every **{day_str}** at **{time_str}**")
                else:
                    time_str = display_dt.strftime('%A at %I:%M %p %Z')
                    meeting_descriptions.append(f"**{i+1}.** Every **{time_str}** (weekly)")
            else:
                time_str = display_dt.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                meeting_descriptions.append(f"**{i+1}.** **{time_str}** (one-time)")
        
        embed.add_field(
            name="✅ Meetings to Schedule:",
            value="\n".join(meeting_descriptions),
            inline=False
        )
    
    # Show failed meetings
    if failed_meetings:
        embed.add_field(
            name="❌ Failed to Parse:",
            value="\n".join(f"• {failure}" for failure in failed_meetings[:5]),  # Limit to 5
            inline=False
        )
    
    if scheduled_meetings:
        view = MultiMeetingConfirmationView(
            author=ctx.author,
            scheduled_meetings=scheduled_meetings,
            channel_id=ctx.channel.id
        )
        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send(embed=embed)

@bot.command(name='schedule', aliases=['sched'], help='Schedules a meeting using natural language. Ex: !schedule tomorrow at 5pm')
async def schedule(ctx, *, time_text: str):
    """Schedules a meeting using natural language parsing, triggered by a command."""
    author_id = str(ctx.author.id)
    active_timezone = user_timezones.get(author_id, SERVER_TIMEZONE)
    
    # Check if this is a multi-meeting command (contains "and" or multiple commas)
    if " and " in time_text.lower() or time_text.count(',') >= 1:
        await handle_multiple_meetings(ctx, time_text, author_id, active_timezone)
        return

    # --- Pre-process to correct misspellings of weekdays ---
    words = time_text.lower().split()
    corrected_words = []
    for word in words:
        # Very permissive distance calculation based on word length
        if len(word) <= 3:
            adaptive_max_distance = 0  # Short words must be exact
        elif len(word) == 4:
            adaptive_max_distance = 2  # 4-char words allow 2 errors
        elif len(word) <= 6:
            adaptive_max_distance = 3  # 5-6 char words allow 3 errors
        else:
            adaptive_max_distance = 5  # 7+ char words allow 5 errors
        
        best_match, _ = scheduling_trie.search_best_match(word, max_distance=adaptive_max_distance)

        if best_match:
            corrected_words.append(best_match)
        else:
            corrected_words.append(word)
    corrected_text = " ".join(corrected_words)

    # --- Weekday Parsing for Complex Recurrence (using the full corrected text for context) ---
    weekday_map = {"monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH, "friday": FR, "saturday": SA, "sunday": SU}
    found_day_consts = set()
    for day_str, day_const in weekday_map.items():
        if re.search(r'\b' + day_str + r'\b', corrected_text):
            found_day_consts.add(day_const)
    found_weekdays = list(found_day_consts)

    # --- Determine Recurrence ---
    # We check for single-day recurrence differently now.
    is_recurring = "every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text or len(found_weekdays) > 1
    
    # If a recurring keyword is used with a single day, it implies weekly recurrence on that day.
    is_single_day_recurrence = (
        ("every" in corrected_text or "weekly" in corrected_text or "recurring" in corrected_text) 
        and len(found_weekdays) == 1
    )

    recurrence_details = None
    if is_recurring or is_single_day_recurrence:
        # If specific days are mentioned (one or more), use them.
        if found_weekdays:
            recurrence_details = {'type': 'specific_days', 'days': found_weekdays}
        else: # Otherwise, it's a simple weekly recurrence (e.g., "!sched every 2pm")
            recurrence_details = {'type': 'simple_weekly'}

    # We now pass the entire corrected text to search_dates, making it more robust.
    current_time = datetime.datetime.now(ZoneInfo(active_timezone))
    found_dates = search_dates(
        corrected_text,
        settings={
            'TIMEZONE': active_timezone,
            'TO_TIMEZONE': active_timezone,  # Keep it in user timezone first
            'RETURN_AS_TIMEZONE_AWARE': True,
            'RELATIVE_BASE': current_time
        }
    )
    
    if not found_dates:
        await ctx.send("I couldn't understand the time. Please try something like 'tomorrow at 3pm' or 'in 2 hours'.")
        return

    _, meeting_time_local = found_dates[0]

    # Convert from user timezone to UTC for storage
    if meeting_time_local.tzinfo is None:
        # If dateparser didn't return timezone info, assume user timezone
        meeting_time_local = meeting_time_local.replace(tzinfo=ZoneInfo(active_timezone))
    
    meeting_time_utc = meeting_time_local.astimezone(ZoneInfo("UTC"))
    
    # --- Manually handle dates in the past ---
    # If the parsed date is in the past, advance to next occurrence instead of next year.
    now_utc = datetime.datetime.now(ZoneInfo("UTC"))
    if meeting_time_utc < now_utc:
        if recurrence_details and recurrence_details['type'] == 'specific_days':
            # For recurring meetings, find the next occurrence of the specified day at the same time
            # Get the time components from the original LOCAL time, not UTC
            target_hour = meeting_time_local.hour
            target_minute = meeting_time_local.minute
            target_second = meeting_time_local.second
            
            # Start from the current time and find the next occurrence
            next_meeting_time = now_utc.replace(hour=target_hour, minute=target_minute, second=target_second, microsecond=0)
            
            # If we've passed that time today, advance to the next occurrence
            if next_meeting_time <= now_utc:
                # Find the next occurrence of any of the specified days
                for day_const in recurrence_details['days']:
                    # Calculate days until next occurrence of this day
                    current_weekday = now_utc.weekday()
                    target_weekday = day_const.weekday
                    days_ahead = (target_weekday - current_weekday) % 7
                    if days_ahead == 0 and next_meeting_time <= now_utc:
                        days_ahead = 7  # If it's the same day but time has passed, go to next week
                    
                    next_occurrence = now_utc + relativedelta(days=days_ahead, hour=target_hour, minute=target_minute, second=target_second, microsecond=0)
                    if next_occurrence > now_utc:
                        meeting_time_utc = next_occurrence
                        break
                else:
                    # If no valid next occurrence found, advance by a week
                    meeting_time_utc += relativedelta(weeks=1)
            else:
                meeting_time_utc = next_meeting_time
        else:
            # Instead of advancing by a year, advance by weeks until we get a future date
            while meeting_time_utc < now_utc:
                meeting_time_utc += relativedelta(weeks=1)

    # Convert the UTC meeting time back to the user's active timezone for a clear confirmation message.
    display_tz = ZoneInfo(active_timezone)
    display_dt = meeting_time_utc.astimezone(display_tz)
    display_time_str = display_dt.strftime('%A, %B %d, %Y at %I:%M %p %Z')
    
    confirm_message = f"Schedule meeting for **{display_time_str}**"

    if recurrence_details:
        if recurrence_details['type'] == 'specific_days':
            # Create a readable list of days
            day_names = {v: k.capitalize() for k, v in weekday_map.items()}
            # Sort by weekday value (0=Monday, 1=Tuesday, etc.)
            sorted_days = sorted(recurrence_details['days'], key=lambda x: x.weekday)
            day_list_str = ", ".join([day_names[day] for day in sorted_days])
            
            time_str = display_dt.strftime('%I:%M %p %Z')
            date_str = display_dt.strftime('%B %d, %Y') # e.g., "July 22, 2025"
            
            # The message is customized to be clearer for recurring meetings on specific days.
            confirm_message = f"Schedule a recurring meeting for every **{day_list_str}** at **{time_str}**? The first meeting will be on **{date_str}**."

        else: # Simple weekly
            confirm_message += " (recurring weekly)?"
    else:
        confirm_message += "?"

    # Pass the timezone-aware UTC datetime to the view.
    view = ConfirmationView(author=ctx.author, meeting_time=meeting_time_utc, channel_id=ctx.channel.id, recurrence_details=recurrence_details)
    await ctx.send(confirm_message, view=view)

@bot.command(name='meetings', aliases=['meeting'], help='Lists all upcoming meetings.')
async def list_meetings(ctx):
    """Displays an interactive list of all scheduled meetings."""
    view = MeetingManagementView(author=ctx.author)
    if not view.meeting_select:
        await ctx.send("There are no meetings scheduled.")
        return
    await ctx.send("Select a meeting from the dropdown below to manage it.", view=view)


@bot.command(name='timecheck', help='Checks the current time from the bot\'s perspective.')
async def timecheck(ctx):
    """Provides a diagnostic of the bot's current time and timezone settings."""
    utc_now = datetime.datetime.now(ZoneInfo("UTC"))
    try:
        server_tz = ZoneInfo(SERVER_TIMEZONE)
    except Exception:
        await ctx.send(f"❌ Error: `{SERVER_TIMEZONE}` is not a valid timezone name.")
        return
    server_now = utc_now.astimezone(server_tz)
    unix_timestamp = int(utc_now.timestamp())
    embed = discord.Embed(title="Time Check Diagnostic", description="Here is the current time information from my perspective.", color=discord.Color.gold())
    embed.add_field(name="My Configured Timezone", value=f"`{server_tz}`", inline=False)
    embed.add_field(name="Current Universal Time (UTC)", value=f"`{utc_now.strftime('%Y-%m-%d %H:%M:%S %Z')}`", inline=False)
    embed.add_field(name="My Calculation for Current Server Time", value=f"`{server_now.strftime('%Y-%m-%d %H:%M:%S %Z')}`", inline=False)
    embed.add_field(name="How Your Discord Client Displays This Moment", value=f"<t:{unix_timestamp}:F> (This should match the time above)", inline=False)
    await ctx.send(embed=embed)

# --- Background Task ---
async def check_meeting_reminders():
    """Background task to check for meetings and send reminders."""
    await bot.wait_until_ready()
    
    # Keep track of reminders we've already sent to avoid duplicates
    sent_reminders = set()
    
    while not bot.is_closed():
        now = datetime.datetime.now(ZoneInfo("UTC"))
        
        for meeting in list(meetings):
            if meeting.get('type') == 'one-time':
                # Handle one-time meetings
                meeting_time = meeting['time'].replace(tzinfo=ZoneInfo("UTC"))
                reminder_time = meeting_time - datetime.timedelta(hours=1)
                reminder_key = f"{meeting['id']}_onetime"
                
                if reminder_time <= now < meeting_time and reminder_key not in sent_reminders:
                    channel = bot.get_channel(meeting['channel_id'])
                    if channel:
                        unix_timestamp = int(meeting_time.timestamp())
                        await channel.send(f"**⏰ REMINDER:** The meeting scheduled by {meeting['requester']} is starting <t:{unix_timestamp}:R>!")
                        sent_reminders.add(reminder_key)
                
                # Remove old one-time meetings
                elif now > meeting_time + datetime.timedelta(hours=1):
                    if meeting in meetings:
                        meetings.remove(meeting)
                        
            elif meeting.get('type') == 'recurring':
                # Handle recurring meetings with exceptions
                instances = get_next_meeting_instances(meeting, from_time=now, limit=5)
                
                for instance_time in instances:
                    reminder_time = instance_time - datetime.timedelta(hours=1)
                    reminder_key = f"{meeting['id']}_{instance_time.isoformat()}"
                    
                    if reminder_time <= now < instance_time and reminder_key not in sent_reminders:
                        # Check if this instance has been cancelled
                        exception = get_meeting_exception(meeting['id'], instance_time)
                        if exception and exception['type'] == 'cancelled':
                            continue  # Skip cancelled instances
                        
                        # For rescheduled instances, use the new time
                        actual_meeting_time = instance_time
                        if exception and exception['type'] == 'rescheduled':
                            actual_meeting_time = exception['new_time']
                            # Recalculate reminder time for rescheduled meetings
                            reminder_time = actual_meeting_time - datetime.timedelta(hours=1)
                            if not (reminder_time <= now < actual_meeting_time):
                                continue
                        
                        channel = bot.get_channel(meeting['channel_id'])
                        if channel:
                            unix_timestamp = int(actual_meeting_time.timestamp())
                            message = f"**⏰ REMINDER:** The recurring meeting scheduled by {meeting['requester']} is starting <t:{unix_timestamp}:R>!"
                            
                            # Add note if this instance was rescheduled
                            if exception and exception['type'] == 'rescheduled':
                                message += " *(This instance was rescheduled)*"
                            
                            await channel.send(message)
                            sent_reminders.add(reminder_key)
        
        # Clean up old reminder keys (older than 24 hours) to prevent memory buildup
        current_time_str = now.isoformat()
        sent_reminders = {key for key in sent_reminders 
                         if not (key.endswith("_onetime") or 
                                key.split("_", 1)[1] < (now - datetime.timedelta(days=1)).isoformat())}
        
        await asyncio.sleep(60)

# --- Bot Startup ---
@bot.event
async def on_ready():
    global nlp
    print(f'{bot.user} has connected to Discord!')
    print("Loading spaCy model...")
    nlp = spacy.load("en_core_web_sm")
    print("Model loaded.")
    load_user_timezones()
    bot.loop.create_task(check_meeting_reminders())

bot.run(TOKEN) 