# Agent Instructions

These instructions apply to any AI coding agent working on this repository. Follow
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, commit titles, and PR guidelines.

## Pull Requests and CodeRabbit Reviews

CodeRabbit reviews every pull request against `main`. A PR is not finished until every
CodeRabbit concern has been addressed. The `main` ruleset blocks merging while any review
thread is unresolved, but resolving a thread is a statement that the concern was handled,
so never resolve a thread you have not addressed.

1. **Wait for the review.** After opening the PR or pushing new commits, wait until
   CodeRabbit finishes. It first posts a summary comment with an in-progress notice and
   edits that same comment when it is done. When it has concerns it also submits a review
   (`coderabbitai[bot]` in the PR reviews). When it has none, it submits no review and the
   summary comment says "No actionable comments were generated". The summary also shows
   which commit it reviewed ("up to `<SHA>`"); make sure that is your latest push. Do not
   merge or report the PR as done before the review for your latest commit is finished.
2. **Collect every concern.** Read all three places where CodeRabbit leaves feedback:
   - Inline review threads (listed by the GraphQL query below).
   - Review bodies, which hold "Outside diff range" and "Nitpick" comments in collapsed
     sections. These are not threads, so the merge block does not catch them.
   - PR conversation comments from `coderabbitai`.
3. **Verify each concern against the code.** Do not apply suggestions blindly, and do not
   dismiss them without a reason. Check whether the problem is real in this codebase,
   including on Windows, macOS, and Linux.
4. **Act on each concern:**
   - Valid: fix it in a new commit, add or update a test when behavior changes, and reply
     with what changed and the commit SHA.
   - Not valid or out of scope: reply with the technical reason. For a valid concern
     that belongs in a separate change, open an issue and link it.
   - Inline-thread concerns: reply in the thread, then resolve it.
   - Concerns without a thread (nitpicks, outside-diff comments, conversation
     comments): reply in one PR conversation comment that lists each concern and its
     outcome. There is nothing to resolve for these.
5. **Repeat.** CodeRabbit reviews new pushes again. Repeat from step 1 until a review
   brings no new actionable comments and CI is green.
6. **Report.** Summarize each CodeRabbit concern and its outcome (fixed, rejected with
   reason, or deferred to an issue) in the PR or to the person who asked for the change.

Useful commands (run from the repository; `gh` fills in `{owner}` and `{repo}`):

```sh
# Reviews and their bodies (look for "Actionable comments", nitpicks, outside-diff comments)
gh api --paginate repos/{owner}/{repo}/pulls/<PR>/reviews --jq '.[] | select(.user.login | test("coderabbit")) | {id, state, body}'

# Review threads with resolution state (--paginate follows endCursor through every page)
gh api graphql --paginate -F owner='{owner}' -F name='{repo}' -F pr=<PR> -f query='
  query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100, after: $endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes { id isResolved path line
            comments(first: 100) { totalCount nodes { databaseId author { login } body } } }
        }
      }
    }
  }'

# A thread whose comments.totalCount is over 100: read all of its comments before resolving it
gh api graphql --paginate -F id=<THREAD_ID> -f query='
  query($id: ID!, $endCursor: String) {
    node(id: $id) { ... on PullRequestReviewThread {
      comments(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId author { login } body } } } }
  }'

# Reply to an inline comment (use the first comment's databaseId)
gh api repos/{owner}/{repo}/pulls/<PR>/comments/<COMMENT_ID>/replies -f body='Fixed in <SHA>: ...'

# Resolve a thread (use the thread id from the query above)
gh api graphql -F id=<THREAD_ID> -f query='
  mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'
```

## Code Scanning and SonarCloud

GitHub code scanning (CodeQL, posted by `github-advanced-security[bot]`) and SonarCloud
(`sonarqubecloud[bot]`) also review every pull request. Their findings must be addressed
before merging, with the same rigor as CodeRabbit concerns:

- The `main` ruleset blocks merging when a PR introduces CodeQL alerts (errors and
  warnings, or security alerts of medium severity or higher) and when the
  `SonarCloud Code Analysis` check fails.
- Address every alert or issue the PR introduces, including notes and minor Sonar
  issues: fix it, or explain in the PR why it is a false positive. Do not dismiss an
  alert in GitHub or mark a Sonar issue as accepted or false positive without stating
  the reason in the PR.
- CodeQL comments appear as inline review threads; reply and resolve them like
  CodeRabbit threads once the fix is pushed and the alert is closed.
- Pre-existing problems on `main` are not a reason to block a PR, but do not make them
  worse; fix them in separate PRs.

```sh
# Open CodeQL alerts on the PR merge ref
gh api --paginate 'repos/{owner}/{repo}/code-scanning/alerts?state=open&ref=refs/pull/<PR>/merge' --jq '.[] | "\(.number) \(.rule.id) \(.rule.severity) \(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'

# Open SonarCloud issues and quality gate status for the PR (public project, no token needed)
curl -s 'https://sonarcloud.io/api/issues/search?componentKeys=ERamaM_ULTRON&pullRequest=<PR>&resolved=false&ps=500'
curl -s 'https://sonarcloud.io/api/qualitygates/project_status?projectKey=ERamaM_ULTRON&pullRequest=<PR>'
```

## Codex Reviews

Codex (`chatgpt-codex-connector[bot]`) also reviews pull requests: a code review and a
security review. Treat its findings exactly like CodeRabbit's: verify each one, fix it or
reply with the technical reason, and resolve its threads only after addressing them.

- Codex reacts with 👀 when it starts and keeps its status in a PR comment containing
  `codex-review-state:v1 {...}`. Wait until both `code_review` and `security_review` report
  a finished status for your latest commit before merging, and do not merge while either
  has unresolved findings.
- Request a new Codex review after pushing fixes by commenting `@codex review`.

```sh
# Codex review state for a PR
gh api --paginate 'repos/{owner}/{repo}/issues/<PR>/comments' --jq '.[] | select(.user.login | test("codex"; "i")) | .body' | grep -o 'codex-review-state:v1 {.*}' | tail -1
```

You can also ask CodeRabbit questions or request a new review by mentioning
`@coderabbitai` in a PR comment.
