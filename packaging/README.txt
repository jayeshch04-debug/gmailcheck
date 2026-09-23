GmailCheck: which of your Gmail addresses still forward to your master mailbox?
===============================================================================

FOLDER LAYOUT

  gmailcheck.exe      double-click to run
  accounts.csv        your login details (edit this first)
  emails\             put your list(s) of addresses here (.txt or .csv)
  success\            forwarding.csv / forwarding.txt  <- addresses that forward
  failed\             not_forwarding.csv               <- the rest, with the reason
  data\               progress (created automatically, don't delete mid-run)

SETUP

1. Turn on 2-Step Verification on both Google accounts below, then create an
   app password for each at https://myaccount.google.com/apppasswords

2. Open accounts.csv (Excel or Notepad) and fill in two rows:

     master  = the Gmail everything forwards TO (it is read over IMAP)
     sender  = a DIFFERENT account that sends the test emails

   The sender must not be the master account. Gmail silently drops forwarded
   copies of mail you sent yourself, so every address would look dead.

   Sending from your catchall domain instead of Gmail? On the sender row, fill in
   smtp_host (e.g. mail.yourdomain.com) and smtp_port (587, or 465 for SSL). Add
   imap_host too if you want bounces (deleted accounts) detected.

3. Put your address list in the emails folder. A .txt needs one address per
   line. A .csv needs an "email" column, or it uses the first column that
   contains @.

RUNNING

Double-click gmailcheck.exe. It will:
  - send up to 400 test emails (Gmail's limit is about 500 a day),
  - wait up to 15 minutes for them to arrive in the master mailbox,
  - write the results into success\ and failed\.

With more than 400 addresses, just double-click it again the next day. It
remembers what it has already sent and carries on with the rest. The files in
success\ and failed\ always hold the full, up-to-date results.

Advanced options (run from a Command Prompt in this folder):
  gmailcheck.exe folder --wait 1800 --daily-cap 450 --cleanup
  gmailcheck.exe folder --dry-run         (show what would be sent, send nothing)
  gmailcheck.exe folder --resend-failed   (retry addresses that didn't arrive)
  gmailcheck.exe --help

STATUS VALUES (in the CSVs)

  forwarding     the test email arrived in the master mailbox
  bounced        the address rejected the email (account deleted or disabled)
  not_received   no bounce, but nothing arrived: forwarding is off or broken
  sent / pending not decided yet; run it again later
