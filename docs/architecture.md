# Architecture and access rules

The application uses Django for sessions, authorization, forms, server-rendered pages, and transactional database operations. PostgreSQL is the production source of truth. JavaScript adds upload interaction, batch-link copying, and screen privacy behavior; client-side controls never grant access.

```mermaid
flowchart LR
    M[Manager browser] -->|HTTPS and personal login| A[Apache]
    R[Rep browser] -->|HTTPS and personal login| A
    A -->|Loopback only| W[Gunicorn / Django]
    W -->|Loopback only| P[(PostgreSQL)]
    U[Excel upload] --> I[Import report]
    I --> D[Scheduled days]
    D --> B[Rep batches]
    B --> N[Unique phone registry and assignment history]
```

## Import and allocation

Numbers are normalized before comparison, so punctuation or a leading country code cannot create a new contact. A permanent unique phone registry prevents a later upload from resetting an existing number. Import rows retain their result, including prior-upload warnings and invalid data. The spreadsheet is not published as a downloadable file.

Every new upload has a required GFS Texting or Ringcentral Texting type. RepListMembership stores each account's eligibility; one rep can belong to both groups. Allocation happens after validation and exclusion. An upload is split among the selected dates; each day's allocation is then divided among selected active group members. Both preview and publication validate membership on the server, including retries. Membership changes use the same PostgreSQL transaction lock as allocation. When a count is uneven, extra rows are distributed deterministically. Every eligible phone is reserved once, including phones scheduled for future dates. The unique registry spans both list types.

The application has two meanings of access:

- A manager can review imports, create batches, manage reps, view history, and clear lists.
- A rep can read phone numbers only from their own batches for today while still belonging to the upload's texting group. The weekly calendar shows only their own schedule metadata (title, type, date, count), including upcoming dates. The date is computed on the server in the configured company timezone. Future, past, and cleared phone lists are inaccessible. URLs are not credentials, and knowing another batch's identifier grants no access.

The batch-link control is a convenience for sharing an authenticated URL. No password, login token, or destination phone number belongs in that URL.

## Read-only lists, clearing, and history

Reps receive read-only lists of phone numbers. There are no call-start, interest, or outcome controls. Assignment means a number was allocated to a rep, not that the rep contacted it. The interface does not display call metrics or completion progress.

An import cannot make a number already in the registry available again. Previously uploaded numbers appear in the import report and are excluded from new allocation. Clearing, date expiry, and reuploading do not automatically reassign existing numbers.

Clearing a batch ends rep access to that batch. Clearing an upload ends rep access to all of its batches, including future dates. Already-open pages hide their list when their status check detects the change; subsequent server requests deny access immediately. Clearing preserves uploads, phone history, assignments, and actor/timestamp audit records. It cannot retract information a rep has already seen.

Earlier versions may contain call or outcome records. Those records remain historical data. They do not give a rep access to a cleared or expired batch, including when an old assignment was marked in progress. The current application does not create or complete calls.

## Trust boundaries

Apache accepts HTTPS requests from the configured private subnet, overwrites proxy headers, and forwards only to loopback Gunicorn. PostgreSQL also listens only on loopback. The application verifies authentication, role, ownership, and dates on the server rather than depending on hidden buttons or client-side time.

Rep responses contain only that rep's schedule metadata and, on authorized list pages, phone numbers available today. Authenticated pages use cache restrictions; password/session protections apply in production. Removing membership or disabling an account revokes access on subsequent requests and invalidates displayed batches on their next status check. Privacy blur and watermarks are deterrents. They cannot provide screenshot prevention or control another application, operating-system capture, or a camera.

Accounts and password resets are administrator-managed. A rep cannot access either the account-management endpoints or Django's self-service password-change endpoints. Generated secrets use Python's secrets module and Django's password hashing; plaintext exists only in the immediate admin response or initial private provisioning file. Resetting a password invalidates prior authenticated sessions. Successful sign-in events record the actor and timestamp; the admin account page shows these events to the second in the company timezone. Merely viewing an account or an active session is not logged as another sign-in.

Audit events support operational accountability, but database administrators can change the database. This is not a tamper-proof compliance archive. Administrative OS/database access should be limited and backups retained under the company's data-retention policy.

## Validation before real use

The production release gate includes authentication/role checks, another rep's direct URL access, future/past-date bypass attempts, normalized reuploads, simultaneous allocation, read-only rep lists, and clearing a batch while its page is open. Verify that legacy in-progress records grant no exception to date or clear restrictions and that old call-action endpoints cannot mutate data. Use PostgreSQL for concurrent tests. Verify HTTPS, static assets, cookies, logs, backup restoration, and service restart on the actual Mac mini before uploading live phone lists.
