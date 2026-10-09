# Docker로 실행하기

tokmo-v3는 POSIX 기능(`fcntl`, 0600 권한 검사)을 쓰므로 Windows에서는 네이티브로 실행되지 않습니다. `docker/` 폴더의 구성은 프로젝트 전체를 Linux 컨테이너 하나로 감싸서, macOS·Linux·Windows(Docker Desktop) 어디서나 같은 방법으로 실행하게 합니다. 모듈과 스케줄의 의미는 [설치와 모듈 설정](setup.md)과 같으며, 이 문서는 컨테이너에서 달라지는 점만 설명합니다.

모든 명령은 **프로젝트 루트**에서 실행합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `docker/Dockerfile` | Python 3.12(Debian bookworm) 위에 zsh·sqlite3·curl·git, supercronic(컨테이너용 cron), 코덱스 CLI, 클로드 CLI(`~/.local/bin/claude`)를 설치하고 `.venv`를 만듭니다 |
| `docker/entrypoint.sh` | 자격증명·승인 파일을 0600 일반 파일로 제자리에 놓은 뒤 `scheduler`/`test`/`gate`/`keys` 중 하나를 실행합니다 |
| `docker/crontab` | `ops/cron/crontab.example`과 같은 잡 목록입니다. **전부 주석 처리된 상태로 시작합니다** |
| `docker/compose.yaml` | 스케줄러 서비스 `tokmo`와 볼륨 없는 테스트 서비스 `test` |
| `docker/tokmo.env.example` | 텔레그램 값(스톡봇·실패 알림) 템플릿. `docker/tokmo.env`로 복사합니다(git 제외) |

## 데이터는 이미지가 아니라 볼륨에 둡니다

코드는 이미지에 들어가고, 상태·자격증명·CLI 로그인은 이름 있는 볼륨에 남습니다. 컨테이너를 지우고 다시 만들어도 볼륨은 유지됩니다.

| 볼륨 | 컨테이너 경로 | 내용 |
|---|---|---|
| `var` | `~/tokmo-v3/var` | 상태 DB, 설정 JSON(`var/data/*.json`), 실행 로그, 회전매매 장부 |
| `secrets` | `~/tokmo-v3/.secrets` | `config/`, `hermes-credentials/`, `env.toss`, `env.kis`, `product-approvals/` |
| `hermes` | `~/.hermes` | 제품 게이트 스크립트, GLM 키(`.env`), 쿼터 스냅샷 |
| `codex`, `claude` | `~/.codex`, `~/.claude` | CLI 로그인 상태 |
| `share` | `~/.local/share/tokmo-v3` | 일부 도구의 기본 저장 경로 |

호스트 폴더를 바인드 마운트하지 않는 이유가 있습니다. 어댑터는 0600 일반 파일이 아닌 자격증명을 거부하는데, Windows·macOS 호스트 폴더는 컨테이너 안에서 그 권한을 보장하지 못합니다.

`.env.toss`와 `.env.kis`는 프로젝트 루트의 단일 파일이어야 해서 볼륨으로 만들 수 없습니다. 대신 `secrets` 볼륨에 `env.toss`, `env.kis`(앞의 점 없음)로 넣어 두면 컨테이너가 시작할 때마다 루트로 복사합니다.

## 처음 설정

```bash
docker compose -f docker/compose.yaml build
cp docker/tokmo.env.example docker/tokmo.env        # 값 채우기 (선택)

# 템플릿을 secrets 볼륨에 펼치고 셸로 들어가 값을 채웁니다
docker compose -f docker/compose.yaml run --rm tokmo bash -c '
  cp -Rn config/env.example/config .secrets/config
  mkdir -p .secrets/hermes-credentials
  cp -n config/env.example/hermes.env ~/.hermes/.env
  cp -n config/env.example/env.toss.example .secrets/env.toss
  cp -n config/env.example/env.kis.example .secrets/env.kis
  chmod -R go-rwx .secrets'
docker compose -f docker/compose.yaml run --rm tokmo bash   # 편집 후 exit (nano/vi 없음: cat > 파일 또는 docker cp 사용)

docker compose -f docker/compose.yaml run --rm tokmo keys   # tools/check_api_keys.py
```

호스트에 있는 파일을 넣을 때는 `docker cp`를 쓰고, 넣은 뒤 권한을 맞춥니다.

```bash
docker compose -f docker/compose.yaml run -d --name tokmo-setup tokmo sleep 600
docker cp ./my-news.env tokmo-setup:/home/tokmo/tokmo-v3/.secrets/config/tokmo/news.env
docker exec tokmo-setup chmod 600 /home/tokmo/tokmo-v3/.secrets/config/tokmo/news.env
docker rm -f tokmo-setup
```

### AI CLI 로그인 (모델 목록에 `codex:`/`claude:`가 있을 때만)

```bash
docker compose -f docker/compose.yaml run --rm tokmo codex login --device-auth
docker compose -f docker/compose.yaml run --rm tokmo /home/tokmo/.local/bin/claude   # 안에서 /login
```

GLM만 쓴다면 로그인이 필요 없습니다. 키는 `hermes` 볼륨의 `~/.hermes/.env`에 둡니다.

### 제품 게이트 (뉴스 판단·모의투자·퀀트를 켜기 전)

```bash
docker compose -f docker/compose.yaml run --rm tokmo gate
```

`install` → `issue --reissue` → `check`를 차례로 실행하고, 승인 파일을 `secrets` 볼륨의 `product-approvals/`에 보관합니다. 승인은 코드 해시에 묶여 있으므로 **코드를 바꿔 이미지를 다시 빌드하면 `gate`를 다시 실행해야 합니다.** 이전 게이트 파일과 내용이 달라 `install`이 거부하면 `gate --force-gate`로 덮어씁니다.

## 실행

1. `docker/crontab`에서 켤 모듈의 줄 주석을 풉니다. 처음에는 제품 게이트 heartbeat와 뉴스만 켜는 것을 권합니다.
2. 스케줄러를 올립니다.

```bash
docker compose -f docker/compose.yaml up -d --build
docker compose -f docker/compose.yaml logs -f tokmo         # supercronic 로그(잡 시작·종료·출력)
docker compose -f docker/compose.yaml exec tokmo bash       # 실행 중인 컨테이너 안에서 점검
```

crontab은 이미지 안에 들어가므로, 줄을 바꾼 뒤에는 `--build`로 다시 올려야 반영됩니다.

시간대는 `TZ=Asia/Seoul`로 고정돼 있어 crontab 시각을 그대로 KST로 읽습니다.

## 테스트

```bash
docker compose -f docker/compose.yaml run --rm test          # 볼륨 없이 깨끗한 컨테이너에서 pytest
docker compose -f docker/compose.yaml run --rm test tests/test_news_rss.py -x
```

## 확인하지 못한 점

- 이 구성으로 실제 API 키를 넣어 잡을 돌려 보지는 않았습니다. 확인한 범위는 이미지 빌드, 도구 설치, 권한, crontab 문법, 테스트 실행까지입니다.
- 코덱스 CLI가 컨테이너 안에서 자체 샌드박스를 쓰려고 할 때 권한이 더 필요할 수 있습니다. 로그인 후 뉴스 판단을 한 번 수동으로 실행해 확인하세요.
- 실주문은 원래와 같이 `var/state/hyundai-rotation/live.flag`가 있을 때만 나갑니다. 처음에는 플래그 없이 드라이런으로 시작하세요.
