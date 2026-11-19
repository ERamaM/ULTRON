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
     in the thread with what changed and the commit SHA.
   - Not valid or out of scope: reply in the thread with the technical reason. For a
     valid concern that belongs in a separate change, open an issue and link it.
   - Then resolve the thread.
5. **Repeat.** CodeRabbit reviews new pushes again. Repeat from step 1 until a review
   brings no new actionable comments and CI is green.
6. **Report.** Summarize each CodeRabbit concern and its outcome (fixed, rejected with
   reason, or deferred to an issue) in the PR or to the person who asked for the change.

Useful commands (run from the repository; `gh` fills in `{owner}` and `{repo}`):

```sh
# Reviews and their bodies (look for "Actionable comments", nitpicks, outside-diff comments)
gh api repos/{owner}/{repo}/pulls/<PR>/reviews --jq '.[] | select(.user.login | test("coderabbit")) | {id, state, body}'

# Review threads with resolution state
gh api graphql -F owner='{owner}' -F name='{repo}' -F pr=<PR> -f query='
  query($owner: String!, $name: String!, $pr: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes { id isResolved path line
            comments(first: 20) { nodes { databaseId author { login } body } } }
        }
      }
    }
  }'

# Reply to an inline comment (use the first comment's databaseId)
gh api repos/{owner}/{repo}/pulls/<PR>/comments/<COMMENT_ID>/replies -f body='Fixed in <SHA>: ...'

# Resolve a thread (use the thread id from the query above)
gh api graphql -F id=<THREAD_ID> -f query='
  mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'
```

You can also ask CodeRabbit questions or request a new review by mentioning
`@coderabbitai` in a PR comment.
