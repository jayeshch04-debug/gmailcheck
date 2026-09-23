# gmailcheck

A CLI that checks which of your Gmail addresses still forward to your master mailbox.

For each address it sends a short test email with a unique reference (`GMC-XXXXXXXXXX`). It then logs in to the master Gmail over IMAP and looks for those references. Addresses whose test email arrived are exported as **forwarding**.

It uses only the Python standard library (Python 3.10 or newer).

## Windows .exe (double-click, folder based)

The **Build Windows exe** GitHub Action builds `gmailcheck.exe` with PyInstaller on every push. Download the `GmailCheck-windows` artifact from the run. Push a `v*` tag and the zip is also attached to a release. The download is a ready-to-use folder:

```
GmailCheck/
  gmailcheck.exe     double-click to run
  accounts.csv       role,email,app_password,smtp_host,smtp_port,imap_host  (master + sender rows)
  emails/            your .txt / .csv lists
  success/           forwarding.csv, forwarding.txt   (written by the program)
  failed/            not_forwarding.csv               (written by the program)
  README.txt         step-by-step instructions
```

When you double-click it (or run `gmailcheck folder`), it sends up to the daily cap, waits for arrivals, and writes `success/` and `failed/`. Run it again on later days to work through a long list. Progress is kept in `data/`. See [`packaging/README.txt`](packaging/README.txt) for the full instructions.

To build it yourself on Windows: `pip install pyinstaller && pyinstaller --onefile --console --name gmailcheck launcher.py`.

## How it decides

| status         | meaning |
|----------------|---------|
| `forwarding`   | The test email arrived in the master mailbox (inbox, archive or spam). |
| `bounced`      | Delivery failed: the SMTP server rejected the address, or a bounce came back. This usually means the account was deleted or disabled. |
| `not_received` | No bounce, and nothing arrived within the grace period. Forwarding is off or broken. |
| `sent`         | Sent, but it's too early to decide. |
| `pending`      | Not sent yet, for example because of the daily cap. |

## Setup

```bash
pip install .            # or: pip install -e '.[test]'
cp .env.example .env     # then fill it in
```

You need two accounts:

1. **Sender**: a separate Gmail account, or your catchall domain's SMTP server.
   **Don't use the master account as the sender.** When mail you sent yourself is forwarded back to you, Gmail throws the copy away, so every address would show up as dead.
2. **Master**: the Gmail account everything forwards to. It needs IMAP access.

For Gmail accounts, turn on 2-Step Verification, then create an app password at <https://myaccount.google.com/apppasswords>.

If you also give it IMAP access to the sender mailbox (`BOUNCE_IMAP_*`), it can tell deleted accounts (`bounced`) apart from broken forwarding (`not_received`).

## Usage

Input is either a `.txt` file with one address per line, or a `.csv` file. For a CSV it uses the `email` column, or else the first column that contains `@`.

One-shot run for small lists:

```bash
gmailcheck run emails.txt --wait 900 -o success.csv --failed failed.csv
```

For large lists, run it in steps across several days. Personal Gmail can send about 500 emails a day and Workspace about 2000. The default cap is 400 per rolling 24 hours.

```bash
gmailcheck send emails.csv            # sends up to --daily-cap, remembers progress
gmailcheck check                      # looks for arrivals and bounces (run any time)
gmailcheck send                       # next day: carries on with the remaining addresses
gmailcheck check --final --cleanup    # decides the stragglers, moves test mails to Trash
gmailcheck export -o success.txt --failed failed.csv
gmailcheck status
```

Useful options:

- `send --dry-run` shows how many emails would go out and prints a sample message.
- `--smtp-preset gmail` fills in the Gmail SMTP host, port and security. You can also set `SMTP_PRESET=gmail`.
- `--delay 3` sets the seconds between sends; some random jitter is added.
- `--limit N` sends at most N emails in this run.
- `send --resend-failed` retries `not_received` addresses with new references.
- `check --grace 30` sets how many minutes `--final` waits before marking an address `not_received`.
- `--db path` sets the state file (default `gmailcheck.db`), so you can keep several lists apart.

Progress is saved after every email. You can stop the program with Ctrl-C and run `send` again later without re-sending anything.

## Tests

```bash
pip install -e '.[test]'
pytest
```
