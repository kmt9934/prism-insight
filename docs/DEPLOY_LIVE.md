# 미니2 라이브 배포 / mini2 live deploy

## 한국어

규칙

1. **파일을 하나씩 복사하지 않습니다.** 코드는 미니1에서 수정하고 commit/push합니다.
2. **항상 commit 전체를 배포합니다.** 미니2에서는 `tools/deploy_live.sh`만 실행합니다.
3. **`.env`에는 값만 둡니다.** 코드는 잘못된 값(공백, 따옴표, `# 주석`, 오타)을 받아도
   경고 로그를 남기고 기본값으로 동작해야 합니다 (`prism_core/env_config.py`).

실행 (미니2, 저장소 폴더에서)

```bash
tools/deploy_live.sh                 # fork/main 배포, 분석 컨테이너만 재생성
tools/deploy_live.sh --with-bot      # 텔레그램 봇 컨테이너도 재생성
tools/deploy_live.sh <commit>        # 특정 commit 배포 / 롤백
```

스크립트 동작: tracked 파일이 수정돼 있으면 거부 → `git fetch fork` →
`git checkout --detach <ref>` → 새 이미지 build → 새 이미지 안에서
`python3 tools/deploy_preflight.py` (네트워크 차단, 주문·텔레그램 없음) →
통과했을 때만 `prism-insight-container` 재생성. 실패하면 이전 checkout으로 되돌리고
컨테이너는 건드리지 않습니다. 원격 이름이 `fork`가 아니면 `PRISM_DEPLOY_REMOTE`로 지정합니다.

처음 한 번 (미니2에 로컬 수정이 남아 있을 때): 지우지 말고 backup branch에 보존한 뒤 배포합니다.

```bash
git switch -c ops/pre-sync-mini2-$(date +%Y%m%d)   # 현재 상태를 보존할 로컬 branch
git commit -am "backup: mini2 local changes before full-commit deploy"   # push하지 않음
tools/deploy_live.sh
```

배포 후에는 첫 정규 cron 실행 결과를 따로 확인합니다 (스모크 통과 ≠ 정규 배치 성공).

**`.env`·`docker/crontab`을 고친 뒤에는 반드시 컨테이너를 재생성합니다.** 파일 하나짜리 마운트라서
편집기가 파일을 새로 바꿔 쓰면 컨테이너는 계속 옛 내용을 읽습니다 (고치기만 해서는 반영되지 않습니다).
`docker compose up -d --force-recreate prism-insight` 후 `tools/check_live_config.sh`로 모두 `OK`인지
확인합니다 (값은 출력하지 않고 파일 이름과 OK/STALE만 표시, `deploy_live.sh` 끝에서도 자동 실행).

## English

Rules

1. **Never copy individual files** onto mini2. Edit on mini1, commit and push.
2. **Always deploy a full commit** with `tools/deploy_live.sh`.
3. **`.env` holds only values.** Code must tolerate bad values (whitespace, quotes,
   trailing `# comments`, typos): log a WARNING and fall back to the default
   (`prism_core/env_config.py`).

Usage (on mini2, in the repo): `tools/deploy_live.sh [--with-bot] [REF]` (REF defaults to
`fork/main`; set `PRISM_DEPLOY_REMOTE` if the remote is not named `fork`).

The script refuses a tree with tracked modifications, fetches, checks out REF detached,
builds a candidate image, runs `tools/deploy_preflight.py` inside that image with the tree
mounted and networking disabled, and only then recreates `prism-insight-container`
(plus `prism-telegram-bot` with `--with-bot`). On failure it restores the previous checkout
and leaves the containers untouched.

First sync with leftover local edits: preserve them on a local backup branch
(`git switch -c ops/pre-sync-mini2-<date> && git commit -am "backup ..."`, not pushed),
then run the script. After deploying, check the first scheduled cron run separately.

**After editing `.env` or `docker/crontab`, recreate the container** — editing alone does not take
effect: these are single-file bind mounts, and an editor that replaces the file leaves the container
on the old inode. Run `docker compose up -d --force-recreate prism-insight`, then
`tools/check_live_config.sh` (names + OK/STALE only, never values; also run at the end of `deploy_live.sh`).
