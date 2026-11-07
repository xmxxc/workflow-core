# Pinned copies

`ai-coding` and `live-verify` are byte-for-byte copies of the user's skills repository (`/path/to/skills-repo`, commit `9d38b84`). This repository keeps them so that `surper-coding` and its shared rules can be used on their own: `surper-coding` follows the ledger, review, release and label rules of `ai-coding`, and hands formal merge and live acceptance to `live-verify`.

- Do not edit these copies here. Workflow changes go into `surper-coding` (or `router.py`). Changes to the prototypes go to the skills repository first, and then get re-copied here with a new pin.
- `wfc install-skills` always installs `surper-coding`, and installs `ai-coding` and `live-verify` only where the user has none of their own, so a user's own copies are never replaced and the two never compete for the same skill name.
- Other skills these depend on (`code-review`, `implement`, `tdd` and so on) come from the skills repository and are prerequisites.

SHA-256 at pin time:

```
5b5e5d8c950a9c9bc1ebd89fcdcc05914ed08d6bf8cb48349f6ae325492c2d9b  ai-coding/agents/openai.yaml
07e73619495e7d1b3f550f2610c150db2ff755624f02841760450b3af607c417  ai-coding/dispatch/codex.md
723b4ee9b5b1991721f119026323f25dfd80f4b1af30a544359524dbd1913cb9  ai-coding/dispatch/host.md
80b3ea72fe20d646912e4450a001a2972b1f34d210416337784a8c3dd41d0126  ai-coding/dispatch/manual.md
de11888127dd9f60f915201d16a74ec53fe6c0c4ed27ff235a411f5fe9a5ac4f  ai-coding/references/handoff.md
5f3d511897433257242a3e1e8936b4397cb4db74eaef4d5385c7c3f1b1fc462a  ai-coding/references/labels.md
70fcb891313447018296741d685ddc732856ca819725cde205e71cbcff170399  ai-coding/references/ledger.md
edd1d291710d75168273d523f2d13d3a5f46933b5b74ba6457420cd25346e197  ai-coding/references/release.md
ed2851c8c371d0d22015c67fe14d9cab4e1b96a967382b0fc5b68633b86299ac  ai-coding/references/review.md
59b81ec716ff15b8142c9211c948edc8d3bc5995b28d9641b891b8280ad11c40  ai-coding/references/verification.md
26cf6187d0c44f5c87b8611d04f1f0963a18c8a20db5471c97dcb155918f9693  ai-coding/SKILL.md
d9ad52401cb6df3352f67b077a62adbb539f9a6c03670921a1e7ffaf59f5c427  live-verify/agents/openai.yaml
ddb1f240b4b2c17c25843da7d1c16f48e1e32c2013b1a55f2bb2c98c5bd5d92a  live-verify/references/acceptance.md
8a609d928fb6723a816a63b074fc5b52e89b8093a6653d5f0afa1f72876137ab  live-verify/references/merge.md
baed1f96969c2add01a30e8d443b7d057480a8aa2a12ed51bac8334594f63de8  live-verify/references/project-profile.md
c551504c7475350e6a42f45e1d3dd9c82f3d19c5e2a7025cb8c27a6e040431b2  live-verify/references/work-records.md
d698f8ed88ac486e63c6d22dac046bc2fb8a650132e909f49c91acc863526731  live-verify/SKILL.md
```
