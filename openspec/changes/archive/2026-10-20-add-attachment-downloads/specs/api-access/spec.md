# Spec Delta

## MODIFIED Requirements

### Requirement: Direct, authenticated HTTPS access
The tool SHALL send every API request over HTTPS to `https://api.todoist.com/api/v1/`, with the personal API token in an `Authorization: Bearer <token>` header. It SHALL talk to the API directly and SHALL NOT use a Todoist SDK. Requests for attachment files follow the requirement "Token only to Todoist hosts".

#### Scenario: Authenticated request
- **WHEN** the tool sends any request to the API
- **THEN** the request goes to a URL under `https://api.todoist.com/api/v1/` and carries the bearer token header

#### Scenario: Certificate verification
- **WHEN** the server's TLS certificate cannot be verified
- **THEN** the tool does not send the request without verification, reports the error, and the run fails with exit status 1

#### Scenario: Redirect of an API request
- **WHEN** the API answers a request with a redirect
- **THEN** the tool does not follow it

### Requirement: Invalid token
The tool SHALL treat an authentication failure of an API request (HTTP 401, or HTTP 403 with an authentication error) as fatal. It SHALL NOT retry it, and it SHALL tell the user to check `TODOIST_API_TOKEN`. A 401 or 403 from a file host for an attachment file is not an authentication failure; the `attachment-downloads` capability describes it.

#### Scenario: Revoked token
- **WHEN** the API answers a request with HTTP 401
- **THEN** the tool makes no further requests, prints an error that mentions `TODOIST_API_TOKEN`, leaves the stored checkpoint unchanged, and exits with status 1

#### Scenario: Attachment refused by the file host
- **WHEN** a request for an attachment file is answered with HTTP 401
- **THEN** the tool does not report an invalid token and continues the run

## ADDED Requirements

### Requirement: Token only to Todoist hosts
The tool SHALL send the token with a request for an attachment file only when the URL uses HTTPS and its host is `todoist.com` or ends in `.todoist.com`. It SHALL follow at most 5 redirects of such a request, only to HTTPS URLs, and SHALL send no token to the URL a redirect points to, whatever its host.

#### Scenario: File on a Todoist host
- **WHEN** the tool requests an attachment file at `https://files.todoist.com/…`
- **THEN** the request carries the bearer token header

#### Scenario: Redirect to a storage host
- **WHEN** the request for an attachment file is answered with a redirect to `https://storage.example.net/…`
- **THEN** the tool requests that URL without the token header

#### Scenario: Redirect back to a Todoist host
- **WHEN** a redirect of an attachment request points to another URL on `files.todoist.com`
- **THEN** the tool requests it without the token header

#### Scenario: Redirect to plain HTTP
- **WHEN** a redirect of an attachment request points to an `http://` URL
- **THEN** the tool does not follow it, and treats the file as refused

#### Scenario: Too many redirects
- **WHEN** the request for an attachment file is redirected a sixth time
- **THEN** the tool stops following, and treats the file as refused

#### Scenario: Token never logged
- **WHEN** an attachment request or one of its redirects is logged at any verbosity
- **THEN** the log line holds neither the token nor the URL's query string
