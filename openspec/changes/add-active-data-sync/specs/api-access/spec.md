# Spec Delta

## Purpose

Defines how todoist-takeout talks to the Todoist API v1: authentication, rate-limit and retry handling, error reporting, and how features missing from the user's plan degrade to warnings instead of failures.

## ADDED Requirements

### Requirement: Direct, authenticated HTTPS access
The tool SHALL send every request over HTTPS to `https://api.todoist.com/api/v1/`, with the personal API token in an `Authorization: Bearer <token>` header. It SHALL talk to the API directly and SHALL NOT use a Todoist SDK.

#### Scenario: Authenticated request
- **WHEN** the tool sends any request to the API
- **THEN** the request goes to a URL under `https://api.todoist.com/api/v1/` and carries the bearer token header

#### Scenario: Certificate verification
- **WHEN** the server's TLS certificate cannot be verified
- **THEN** the tool does not send the request without verification, reports the error, and the run fails with exit status 1

### Requirement: Invalid token
The tool SHALL treat an authentication failure (HTTP 401, or HTTP 403 with an authentication error) as fatal. It SHALL NOT retry it, and it SHALL tell the user to check `TODOIST_API_TOKEN`.

#### Scenario: Revoked token
- **WHEN** the API answers a request with HTTP 401
- **THEN** the tool makes no further requests, prints an error that mentions `TODOIST_API_TOKEN`, leaves the stored checkpoint unchanged, and exits with status 1

### Requirement: Rate-limit handling
When the API answers with HTTP 429, the tool SHALL wait for the delay the API specifies, then send the same request again. The delay is the `retry_after` value in the error body's `error_extra`, or else the `Retry-After` header, or else a default backoff. The tool SHALL give up when the next wait would push the total rate-limit wait of the run past a configured limit.

#### Scenario: Retry after the given delay
- **WHEN** a request is answered with HTTP 429 and `error_extra.retry_after` is 30
- **THEN** the tool logs a message, waits at least 30 seconds, and repeats the request

#### Scenario: Header only
- **WHEN** a 429 response has no `retry_after` in its body but has a `Retry-After: 12` header
- **THEN** the tool waits at least 12 seconds before repeating the request

#### Scenario: Rate limit persists
- **WHEN** the next 429 wait would push the run's total rate-limit wait past the limit
- **THEN** the tool stops, keeps the last checkpoint, reports the rate limit as the cause, and exits with status 1

### Requirement: Transient error retries
The tool SHALL retry requests that fail with a network error, a timeout, or an HTTP 5xx status, with exponential backoff and a bounded number of attempts. It SHALL NOT retry other HTTP 4xx errors.

#### Scenario: Temporary server error
- **WHEN** the first attempt of a request returns HTTP 503 and the second attempt succeeds
- **THEN** the tool uses the second response and the run succeeds

#### Scenario: Persistent network failure
- **WHEN** every attempt of a request fails with a connection error
- **THEN** the tool stops after the last attempt, keeps the last checkpoint, and exits with status 1

#### Scenario: Client error
- **WHEN** a request is answered with HTTP 400
- **THEN** the tool does not retry it, and reports the API's `error`, `error_tag` and `error_code` in its message

### Requirement: Responses preserved exactly
The tool SHALL parse API responses as JSON without mapping them onto a fixed model. Every field of every object SHALL reach storage, including fields the tool does not know about.

#### Scenario: Unknown field
- **WHEN** an object in a response contains a field that the tool's code does not mention
- **THEN** the stored copy of that object contains the field with the same value

### Requirement: Plan limitations degrade to warnings
When the user's plan does not include a feature that the backup covers, the tool SHALL report a warning that names the feature, skip that part, and finish the rest of the backup with exit status 0. The tool learns this either from the plan information the API returns or from an API error that marks the request as restricted by the plan.

#### Scenario: Comments not on the plan
- **WHEN** the plan information returned by the API says comments are not available
- **THEN** the tool prints a warning that comments cannot be backed up on this plan, backs up everything else, and exits with status 0

#### Scenario: Restricted request
- **WHEN** a request for an optional part of the backup is rejected with an error that marks it as unavailable on the user's plan
- **THEN** the tool prints a warning naming that part, does not retry the request, continues with the rest of the backup, and exits with status 0

#### Scenario: Free account
- **WHEN** the backup runs on a free Todoist account
- **THEN** the run completes with exit status 0
