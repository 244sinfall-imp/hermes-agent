# Raze maintained Hermes patch train

This fork versions the application source actually used by Raze. It is not a copy
of credentials, profile homes, conversations, runtime leases or machine config.

## Branches and ownership

- Official upstream: `NousResearch/hermes-agent`, remote `origin` in the existing
  server checkout (kept compatible with current operator tooling).
- Maintained fork: `244sinfall-imp/hermes-agent`, remote `imp`.
- Runtime-source branch: `raze/managed`. Keep its remote tracking explicit; do not
  infer that `origin/main` is the deployed version.
- Deployed baseline started at `83541d3f7c9d254a47332a285382c25e86d78aba`, including
  the signed source-aware PM currency fix. The previously local profile isolation
  patchset is now a separate signed commit. Its inherited empty approval-test
  draft is preserved, not counted as a new verification test.
- Personal provider overlay is maintained separately in
  `244sinfall-imp/hermes-official-plugins`, branch `raze/personal-directsdk`, under
  `deployments/raze-personal/plugin/`. The generic upstream plugin subtree remains
  intact. Real owner pins and auth files never enter either repository.

## Every-change sync contract

1. Read the current local/remote source and applicable AGENTS before editing.
2. Preserve staged/unstaged/untracked changes losslessly before migration. No
   destructive reset, auto-stash, blanket clean or force-push to normalize drift.
3. Implement in an isolated worktree. Commit with the established agent identity
   and GPG signature; reference the concrete bug and record exact test exits.
4. Keep account/organization/profile ownership mandatory. Subscription tier and
   quota labels are optional observations, not authentication invariants. Never
   add a personal credential fallback to make a work-profile test pass.
5. Publish the exact reviewed commits through SSH and read back remote refs/PRs.
   A local commit or an exit-zero push is not a deployment receipt.
6. Stage source-linked runtime/plugin payloads, preserve rollback, then verify
   actual packaged import origins and a bounded authorized canary before claiming
   installation success. Record the deployed source IDs outside volatile logs.
7. End each change with clean managed source and a verified remote ref. Explain
   any deliberately pending or offline target rather than creating hidden drift.

## Upstream updates are reviewed, not blindly applied

Fetch upstream separately, compare its changes with the maintained branch, and
integrate in a candidate worktree. Reapply/review local profile policy, use the
canonical test runner, and verify packaged runtime behavior. Do not run a broad
`hermes update` over the live tree merely to synchronize Git: it can migrate
profiles/config/plugins and needs its own scope and validation.

A source-only fix does not require pulling thousands of unrelated commits or
restarting the shared gateway. Existing immutable leases stay intact; verify
which process adopted the replacement instead of claiming every running client
hot-reloaded. Never change Work auth or business context as a personal repair.
