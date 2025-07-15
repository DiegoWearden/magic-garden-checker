# 🤖 Discord Meeting Handler Bot

A sophisticated Discord bot for scheduling and managing meetings with natural language processing, smart spell checking, and comprehensive meeting management features.

## ✨ Features

### 🗓️ Smart Meeting Scheduling
- **Natural Language Processing**: Schedule meetings using phrases like "tomorrow at 5pm" or "every Monday at 2pm"
- **Advanced Spell Checking**: Intelligent correction with first-letter prioritization for scheduling terms
- **Multiple Meeting Support**: Schedule several meetings in one command: `!sched every friday at 7pm, every wednesday at 6:30pm`
- **Recurring Meetings**: Support for complex recurring patterns with bulk confirmation

### 📋 Comprehensive Meeting Management
- **Individual Instance Control**: Reschedule or cancel specific instances of recurring meetings
- **Series vs Instance Options**: Choose between modifying the entire recurring series or just one occurrence
- **Interactive Interface**: Dropdown menus for easy meeting selection and management
- **Clear All Option**: Quickly clear your entire meeting schedule with safety confirmation

### 🌍 Timezone Awareness
- **Per-User Timezones**: Set your personal timezone for accurate meeting times
- **Automatic Conversion**: All times displayed in your local timezone
- **Dynamic Timestamps**: Discord's native timestamp display for universal compatibility

### ⏰ Smart Reminders
- **Automatic Notifications**: 1-hour advance reminders for all meetings
- **Exception Handling**: Properly handles cancelled and rescheduled instances
- **Recurring Support**: Intelligent reminder system for recurring meetings

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- Discord Bot Token
- Required Python packages (see `requirements.txt`)

### Installation

1. **Clone the repository**:
   ```bash
   git clone git@github.com:DiegoWearden/meeting-handler-bot.git
   cd meeting-handler-bot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   python -m spacy download en_core_web_sm
   ```

3. **Set up environment**:
   Create a `.env` file with your Discord bot token:
   ```
   DISCORD_TOKEN=your_bot_token_here
   ```

4. **Run the bot**:
   ```bash
   python bot.py
   ```

## 📖 Commands

| Command | Description |
|---------|-------------|
| `!schedule` or `!sched` | Schedule meetings using natural language |
| `!meetings` or `!meeting` | View and manage your scheduled meetings |
| `!setzone` | Set your personal timezone |
| `!timecheck` | Check current time and timezone settings |
| `!help` | Show all available commands |

## 🎯 Usage Examples

### Basic Scheduling
```
!sched tomorrow at 5pm Team meeting
!sched Friday 2pm Project review
!sched next Tuesday at 10am Daily standup
```

### Recurring Meetings
```
!sched every Monday at 9am Weekly sync
!sched every Tuesday at 2pm Team retrospective
!sched weekly Friday 4pm All-hands meeting
```

### Multiple Meetings
```
!sched every friday at 7pm, every wednesday at 6:30pm, and every monday at 6:30pm
!sched make meetings for every tuesday at 2pm, every thursday at 5pm
```

### Meeting Management
- Use `!meetings` to view all scheduled meetings
- Select meetings from dropdown to cancel or reschedule
- Choose between "Just Next Instance" or "All Future Meetings" for recurring meetings
- Use "Clear All Meetings" to reset your entire schedule

## 🔧 Technical Features

### Natural Language Processing
- **spaCy Integration**: Advanced entity recognition for meeting details
- **Spell Correction**: Custom Trie-based spell checker with adaptive distance limits
- **Timezone Parsing**: Intelligent timezone handling with `dateparser` and `zoneinfo`

### Data Management
- **Meeting Exceptions**: Track individual instance modifications without affecting base patterns
- **Persistent Storage**: User timezone preferences saved to JSON
- **Memory Management**: Efficient handling of recurring meeting instances

### Discord Integration
- **Interactive UI**: Rich embeds, dropdowns, buttons, and modals
- **Error Handling**: Comprehensive error handling with user-friendly messages
- **Permission Checks**: User-specific interaction controls

## 📁 Project Structure

```
discord-bot/
├── bot.py              # Main bot application
├── requirements.txt    # Python dependencies
├── .env               # Environment variables (create this)
├── .gitignore         # Git ignore rules
├── user_timezones.json # User timezone storage
└── README.md          # This file
```

## 🧠 Smart Features

### Spell Checking Algorithm
- **Adaptive Distance**: Stricter checking for short words, more lenient for longer words
- **First-Letter Prioritization**: "wendsay" → "wednesday" (not "monday")
- **Context Awareness**: Specialized dictionary for scheduling terms

### Meeting Instance Management
- **Exception Tracking**: Individual meeting modifications stored separately
- **Chronological Sorting**: Instance dropdown sorted by proximity to current time
- **Visual Indicators**: Clear display of cancelled and rescheduled instances

### Reminder System
- **Duplicate Prevention**: Smart tracking to avoid multiple reminders
- **Exception Handling**: Skips cancelled instances, uses new times for rescheduled ones
- **Background Processing**: Continuous monitoring without blocking bot operations

## 🤝 Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## 📄 License

This project is open source and available under the [MIT License](LICENSE).

## 🙏 Acknowledgments

- Built with [discord.py](https://discordpy.readthedocs.io/)
- Natural language processing powered by [spaCy](https://spacy.io/)
- Date parsing by [dateparser](https://dateparser.readthedocs.io/)
- Recurring patterns handled by [python-dateutil](https://dateutil.readthedocs.io/) 