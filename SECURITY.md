# Security

This initial release is for a trusted, single-team instance. It has one shared
Basic-auth account and CSRF-protected web mutations, not user accounts or RBAC.
Only trusted people should be able to create or queue tasks: queuing a task
authorizes a coding agent to execute commands in a mounted repository.

Use HTTPS, a private/restricted ingress, strong credentials and proxy-level
rate limiting. Rotate secrets if exposed. Provider credentials are available
to the execution environment; do not grant agents production credentials.
Containers reduce ambient access but do not make malicious tasks safe.

Do not publish an instance without authentication, mount the Docker socket,
run the worker as root, or expose the repository/data directory over HTTP.
The PHP development server is for localhost use only. Review workspace diffs
and test evidence before merging/deploying agent changes.

Report vulnerabilities privately using this repository's **Security → Report a
vulnerability** page. If private reporting is unavailable, contact the maintainer
through their GitHub profile without including exploit details in public issues.
Only the latest public release receives fixes during the initial development
phase. There is no promised response-time SLA.
