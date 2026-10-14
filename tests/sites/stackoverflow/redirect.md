# What does 2>&1 in this command mean? [duplicate]

## Question

Score: 77

Author: codecowboy

Edited: 2013-11-07 22:24:49Z by Gilles 'SO- stop being evil'

Asked: 2013-11-07 17:23:17Z by codecowboy

__This question already has answers here__:

[How could I remember how to use redirection?](https://unix.stackexchange.com/questions/746/how-could-i-remember-how-to-use-redirection)  (9 answers)

Closed 12 years ago.

I understand that this command attempts to write to nowhere or a null device but what does `2>&1` mean here?

```
wget -q -O - http://yourwebsite.com/wp-cron.php?doing_wp_cron > /dev/null 2>&1
```

### Comment by slm — 2013-11-07 22:38:58 +00:00

Score: 1

Gilles found this one which seems even more like a dup: [unix.stackexchange.com/questions/37660/order-of-redirections](http://unix.stackexchange.com/questions/37660/order-of-redirections "order of redirections")

This comment was edited 1 time

## Answer 99264 by dg99 (accepted)

Score: 126

Author: dg99

Edited: 2018-07-26 21:06:08Z by Hashim Aziz

Answered: 2013-11-07 17:26:51Z by dg99

`2` refers to the second file descriptor of the process, i.e. `stderr`.

`>` means redirection.

`&1` means the target of the redirection should be the same location as the first file descriptor, i.e. `stdout`.

So `> /dev/null 2>&1` first redirects `stdout` to `/dev/null` and then redirects `stderr` there as well. This effectively silences all output (regular or error) from the `wget` command.

::edit:: [Here](http://www.tldp.org/LDP/abs/html/io-redirection.html) is an excellent quick reference for you.

### Comment by codecowboy — 2013-11-07 17:28:31 +00:00

Score: 0

Thanks. When run in a shared hosting cron job which typically sends an email of the output to an admin, would the > /dev/null 2>&1 prevent this from happening do you think? Also do spaces surrounding '>' matter?

This comment was edited 1 time

### Comment by dg99 — 2013-11-07 17:32:34 +00:00

Score: 0

Yes, the complete silencing trick is standard practice to make sure that absolutely no output exists to trigger email from a cron job. (Note, however, that the hosting provider may have done all sorts of strange things to your cron environment to make it send some sort of email regardless of your efforts.) ::edit:: And no, spaces around the lone `>` do not matter, but spaces within the `2>&1` probably do matter (I can't recall).

This comment was edited 1 time

### Comment by Joseph R. — 2013-11-07 17:32:44 +00:00

Score: 9

It might also be worth noting that the same semantics are often achieved with the idiom `&>/dev/null`
