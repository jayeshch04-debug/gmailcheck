GmailCheck: which of your Gmail addresses still forward to your master mailbox?
===============================================================================

FOLDER LAYOUT

  gmailcheck.exe      double-click to run
  accounts.csv        your login details (edit this first)
  emails\             put your list(s) of addresses here (.txt or .csv)
  success\            forwarding.csv / forwarding.txt  <- addresses that forward
  failed\             not_forwarding.csv               <- bounced / never arrived, with reason
                      pending.csv                      <- not sent yet / still waiting
  data\               progress (created automatically, don't delete mid-run)

SETUP

1. Turn on 2-Step Verification on both Google accounts below, then create an
   app password for each at https://myaccount.google.com/apppasswords

2. Open accounts.csv (Excel or Notepad) and fill in the two rows:

     role,email,app_password
     master,your.master@gmail.com,abcd efgh ijkl mnop
     sender,another.account@gmail.com,abcd efgh ijkl mnop

   master = the Gmail everything forwards TO (it is read over IMAP)
   sender = a DIFFERENT account that sends the test emails

   Only those three columns are needed. The sender must not be the master
   account, because Gmail silently drops forwarded copies of mail you sent
   yourself.

   Sending from your catchall domain instead of Gmail? Add the columns
   smtp_host,smtp_port,imap_host and fill them in on the sender row only, e.g.
   mail.yourdomain.com,587,mail.yourdomain.com. Leave them blank for Gmail.

3. Put your address list in the emails folder. A .txt needs one address per
   line. A .csv needs an "email" column, or it uses the first column that
   contains @.

RUNNING

Double-click gmailcheck.exe. It will:
  - check it can reach Gmail and that both logins work (this takes seconds),
  - send in batches of 25, showing each address as it goes,
  - check the master inbox after every batch, showing which ones arrived,
  - pause 15 seconds, then start the next batch (up to 400 a day),
  - after the last batch, keep checking for up to 10 minutes for stragglers,
  - keep success\ and failed\ up to date the whole time.

With more than 400 addresses, just double-click it again the next day. It
remembers what it has already sent and carries on.

IF IT SAYS IT CAN'T REACH THE MAIL SERVER

  "connection timed out" on both 587 and 465 means this computer's network
  blocks outgoing email. That's very common on VPS/cloud servers (AWS, Azure,
  Vultr, OVH...) and sometimes caused by a firewall or antivirus. Run it on a
  home PC/laptop instead, or ask the provider to unblock ports 587/465.

  To test the connection without sending anything, open a Command Prompt in
  this folder and run:   gmailcheck.exe test

Advanced options (run from a Command Prompt in this folder):
  gmailcheck.exe folder --batch-size 50 --batch-pause 30 --daily-cap 450
  gmailcheck.exe folder --wait 1800 --cleanup
  gmailcheck.exe folder --dry-run         (show what would be sent, send nothing)
  gmailcheck.exe folder --resend-failed   (retry addresses that didn't arrive)
  gmailcheck.exe --help

STATUS VALUES (in the CSVs)

  forwarding     the test email arrived in the master mailbox
  bounced        the address rejected the email (account deleted or disabled)
  not_received   no bounce, but nothing arrived: forwarding is off or broken
  sent / pending not decided yet (in pending.csv); run it again later
