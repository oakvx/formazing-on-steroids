# Formazing - Training Management App
![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)

![Personal](https://img.shields.io/badge/Project-Personal-orange)

A clear and linear guide to managing training sessions in 3 clicks, without stress and without errors.

---

## What is this app and why it exists

### The problem it solves

"I need to schedule training sessions for the association, but every time I waste time creating Teams meetings, sending emails, remembering codes, and managing feedback. What if I make a mistake in the communications? Total panic."

### The solution

An app as simple as a button that:

* **Does nothing on its own** - only you decide when to act.
* **Uses Notion as a database** (you enter the data, the app transforms it).
* **Blocks errors** (no accidental sends, no broken links).

**Keyword: Total Control. You command, the app obeys.**

---

## General Architecture

### 1. Notion = Your shared Excel sheet

**What it contains:** Only the raw data of the training sessions (Name, Area, Date...).

**What it DOES NOT contain:** Complex formulas, automations, or calculated codes.

#### Database structure (mandatory fields):

| Field | Example | Notes |
| --- | --- | --- |
| **Name** | Web Security | Title of the training |
| **Area** | IT | Dropdown: IT, R&D, HR, Legal, Commercial, Marketing, All |
| **Date/Time** | 03/15/2024 14:00 | Confirmed date and time |
| **Period** | SPRING | Dropdown: SPRING, AUTUMN, ONCE, EXT, OUT |
| **Status** | Planned | Only 3 options: Planned, Scheduled, Completed |
| **Code** | (empty) | The app fills this at the right time |
| **Teams Link** | (empty) | The app fills this at the right time |

#### Period Types:

* **SPRING/AUTUMN**: Periodic training sessions (Spring/Autumn).
* **ONCE**: Internal one-off training sessions.
* **EXT**: Training WE RECEIVE FROM THE OUTSIDE (e.g., from companies, professionals, or other JEs) to develop new skills.
* **OUT**: Training WE PROVIDE TO THE OUTSIDE (e.g., upon request by other JEs or universities).

---

## Complete Operational Flow

### Phase 1: Create the training in Notion (you, in peace)

1. Open the "Trainings" Notion database.
2. Click "New page".
3. Fill in only these fields:
* **Name** -> Web Security
* **Area** -> IT
* **Date/Time** -> 03/15/2024 14:00
* **Period** -> SPRING
* **Status** -> Planned (mandatory!)


4. Do not touch other fields (Code, Teams Link remain empty).
5. Save -> the training is ready to be sent.

**Why it is safe:**

* No automatic sending.
* You can modify data as long as the status is "Planned".

### Phase 2: Send communications (1 click in the app, with mandatory preview)

1. **Open the Flask app** and log in with Microsoft Entra SSO (allowed domain + explicit administrator list).
2. **See ONLY the training sessions** with status = "Planned".
3. **Select the training** and click "Preview communications".
4. **See exactly** what will be sent and to whom:

#### Preview example:

```
EMAIL (sent to: IT team)
Subject: [IT] "Web Security" Training on 03/15 - Code: IT-Web_Security-2024-SPRING-01

TELEGRAM (group: IT + MAIN)
Message: New training for IT!
Topic: Web Security
Date: 03/15/2024 14:00

```

5. **If everything is OK**, click "CONFIRM SEND".

The app performs **5 things in sequence:**

1. Generates the code -> `IT-Web_Security-2024-SPRING-01`
2. Creates the Teams meeting -> link saved in Notion.
3. Sends emails to the involved areas, including the Teams link.
4. Sends Telegram messages to the involved groups (area group + main group), including the Teams link.
5. Updates the status -> "Scheduled".

### Phase 3: Send feedback (1 click in the app, after the training)

1. **Open the Flask app** -> go to "Trainings to close".
2. **See ONLY the training sessions** with status = "Scheduled".
3. **Click "SEND FEEDBACK"**.

The app performs **3 things:**

1. Searches for the pre-filled link in `feedback_links.csv`.
2. Sends the link via Telegram to the involved groups.
3. Updates the status -> "Completed".

---

## 3-Step Flow Summary

1. **In Notion:** You fill in the basic data -> Status = "Planned".
2. **In the App:** You click "Send communications" -> the app generates the code, creates Teams, sends email/Telegram after preview.
3. **After the Training:** You click "Send feedback" -> the app sends the link via Telegram and updates status to "Completed".

**No hidden complexity. No risk. Only 2 clicks per month.**

---

## Security Guarantees

| Scenario | App Solution | Result |
| --- | --- | --- |
| "I'm afraid of sending by mistake" | Password + mandatory preview + explicit confirmation | No accidental sending |
| "I don't want to manage complex codes" | The app generates the code on click | Zero human error |
| "How do I manage feedback links?" | Separate script generates links offline | Links always ready |
| "A colleague could ruin everything" | Microsoft SSO + admin authorization + CSRF protection | Only you can send |
| "I want to know today's sessions" | Telegram bot with commands | Info always at your fingertips |

---

## Security, log search and local setup

The Flask web app now requires a strong `FLASK__SECRET_KEY` and
Microsoft OAuth `state` verification. Administrative submissions use
CSRF-protected POST forms, and the app binds to `127.0.0.1` by default.
An explicitly exposed deployment requires HTTPS, secure session cookies
and a properly configured Microsoft Entra redirect URI.

**New diagnostic features:** the admin-only log viewer supports severity
filters, case-insensitive search and a bounded 100/200/500-line response
with a truncation notice. It redacts common secret-bearing fields but is
not a substitute for avoiding sensitive information in logs.

Copy [`.env.example`](.env.example) to a private `.env` and read the
[security and deployment guide](docs/security-hardening.md) before
starting. A previously configured session must log in again after
rotating its signing key. The security CI uses synthetic data and makes
no calls to external Microsoft, Notion or Telegram accounts.

---

## Testing and Validation

The project includes a complete testing system to ensure reliability and safety in production.

**For full testing information**: [docs/testing/README.md](docs/testing/README.md)

---

## Documentation

For detailed information on architecture, API, and configuration:

**Full Documentation**: [docs/README.md](docs/README.md)

---

## Project Structure

```
Formazing/
├── app/
│   ├── __init__.py           # Initializes the Flask app
│   ├── routes.py             # Main dashboard and API endpoints
│   ├── services/
│   │   ├── notion/               # Notion Service (modular architecture)
│   │   │   ├── __init__.py       # Facade pattern - Unified API
│   │   │   ├── notion_client.py  # Core connection and authentication
│   │   │   ├── query_builder.py  # Dynamic query construction
│   │   │   ├── data_parser.py    # Data parsing and mapping
│   │   │   ├── crud_operations.py # Database CRUD operations
│   │   │   └── diagnostics.py    # Monitoring and debugging
│   │   ├── bot/                # Telegram bot system
│   │   │   ├── telegram_commands.py  # Bot command handlers
│   │   │   └── telegram_formatters.py # Message formatting
│   │   ├── mgraph_service.py   # Microsoft Graph API (Teams, Email)
│   │   ├── telegram_service.py # Telegram orchestrator
│   │   └── training_service.py # Main orchestrator
│   ├── templates/              # Jinja2 web templates
│   │   ├── layout/               # Base layouts and structures
│   │   │   ├── base.html           # Main base template
│   │   │   └── auth_required.html  # Layout with authentication
│   │   ├── pages/                # Complete pages
│   │   ├── organisms/            # Reusable complex components
│   │   ├── molecules/            # Medium components (forms, cards, etc.)
│   │   ├── atoms/                # Base components (buttons, icons, etc.)
│   │   │   ├── badge.html          # Status badges
│   │   │   ├── button.html         # Buttons
│   │   │   ├── card.html           # Card containers
│   │   │   ├── icon.html           # Icons
│   │   │   └── loading.html        # Loading indicators
│   │   ├── legacy/               # Legacy templates (deprecated)
│   │   └── error.html            # Error page
│   └── static/                 # Static assets
│       └── style.css             # CSS stylesheets
├── tests/
│   ├── conftest.py             # Pytest configuration
│   ├── fixtures/               # Modular fixtures for testing
│   ├── unit/                   # Component unit tests
│   ├── integration/            # Real integration tests
│   ├── e2e/                    # End-to-end workflow tests
│   ├── config/                 # Test configurations
│   └── mocks/                  # Mock services
├── config/
│   ├── telegram_groups.json    # Map Areas -> Telegram Chat IDs
│   └── message_templates.yaml  # Message templates
├── docs/
│   ├── README.md               # General documentation
│   ├── bot-telegram.md         # Bot documentation
│   ├── notion-service.md       # Notion service documentation
│   ├── templates/              # Template system documentation
│   │   └── README.md             # Atomic design and components guide
│   └── testing/                # Testing documentation
│       ├── README.md             # General testing guide
│       ├── fixture-testing-guide.md # Complete fixture guide
│       └── fixture-quick-reference.md # Fixture quick reference
├── quick_test.bat              # Windows test script
├── quick_test.sh               # Linux/Mac test script
├── .env                        # Environment variables
├── config.py                   # Flask configurations
├── requirements.txt            # Python dependencies
└── run.py                      # Application entry point

```

---

**Formazing: training management has never been this simple.**
