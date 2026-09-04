# Submission Statement
## Testing
- starter\Submission\testing\testing_deployed.txt are the tests for the deployed instance.
- Note: accessing https://www.amazon.com was difficult due to bot prevention on the site itself. The agent nonetheless returned a screenshot found in screenshots folder
- Added an extra test for a different website than wwww.amazon.com that successfully returned the website name
- Added the logs showing proper tool calling in starter\Submission\testing\logs.txt

## Reflection
- found in Reflection.md

## Notes on Resubmission
## Browser Tool Fix in Deployment

### 1. Playwright Driver Issue

**Investigation:** The browser tool failed only in the deployed environment, not locally. CloudWatch logs showed `PermissionError: [Errno 13] Permission denied: '/var/task/playwright/driver/node'` when Playwright tried to launch its Node.js driver subprocess.

**Root cause:** `agentcore deploy`'s dependency-build pipeline installs Python packages correctly for Linux ARM64 via `uv`, but has no step to run `playwright install` or otherwise fix binary permissions. The driver binary was the correct size and architecture, but lost its Unix executable bit during Windows→Linux packaging — confirmed by diagnostic logging showing the file present with mode `0o664` (read/write only, no execute).

**Solution:** Since `/var/task` is read-only at runtime (Lambda-style filesystem), we copied the driver binary to `/tmp` (writable), applied `chmod 0o755` there, and pointed Playwright at the copy using its `PLAYWRIGHT_NODEJS_PATH` environment variable override — read directly by `compute_driver_executable()` in Playwright's own source.

### 2. IAM Permission Issue

**Investigation:** After fixing the driver, the browser tool launched but every session failed with an `AccessDeniedException` on `StartBrowserSession`, caught internally by `strands_tools` and surfaced to the LLM only as a vague "authorization issue" — not visible until we enabled DEBUG-level logging for `botocore` and `strands_tools`.

**Root cause:** Our IAM policy scoped Browser permissions to our own account (`arn:...:261384490861:browser/*`), but AWS's actual managed Browser resource lives under a shared, AWS-owned ARN: `arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1` — the same pattern already used for our (working) Code Interpreter permissions.

**Solution:** Updated the execution role's inline policy to scope the Browser permission statement to `arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1`, matching the actual resource AWS uses.

## calculate_loyalty_discount naming convention
### 3. tier_discount_pct 

**Solution:** key was renamed in the code and the returnable json dump 