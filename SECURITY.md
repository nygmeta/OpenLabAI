# Security and safety reports

OpenLabAI sits between a language model and laboratory instruments, so some bugs
are safety problems and not just defects. Examples: a way to make a robot move
without the `confirm` gate, a way around deck or volume validation, an audit log
that reports a record that was not written, or an injection path from LIMS or
Slack content into an instrument command.

Please do not post details of such a problem in a public issue. Use GitHub's
private vulnerability reporting: open the **Security** tab of this repository and
choose **Report a vulnerability**.

Include what you did, what you expected, what happened, and the instrument or mock
mode involved. Reports about behaviour on physical hardware are the most valuable
and should say so.

This is a research project maintained by one person. Expect an acknowledgement,
not a service level.
