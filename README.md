# CasparCG Monitor (by sendust)

헤드리스 Ubuntu에서 동작하는 CasparCG Server 2.5 감시·제어용 웹 페이지.
Python 표준 라이브러리만 사용하며(추가 패키지 없음) 백엔드 `monitor.py` 하나와 정적 페이지 `static/index.html`로 구성된다.

- 대상 환경: Ubuntu 26.04, CasparCG Server 2.5.1 (CEF 142), DeckLink 출력, NVIDIA GPU
- 기본 주소: `http://<서버>:8070/`

## 기능

| 영역 | 내용 |
|---|---|
| 헤더 | AMCP 연결 상태·버전, AMCP 클라이언트 수, OSC 수신 상태, 프로세스 가동 시간, systemd 서비스 상태·재시작 횟수 |
| 제어 버튼 | **Start service / Stop service / Kill CasparCG** — 모두 확인 창 2번 |
| CPU | 모델, 아키텍처, 코어/스레드, load avg, 전체·코어별 사용률, **사용률 그래프** |
| Memory & System | RAM 여유/전체, swap, 시스템 가동 시간, 부팅 시각, 커널 |
| GPU | 이름, 온도, 전력, GPU/NVENC/NVDEC 사용률, VRAM, **사용률 그래프** (nvidia-smi) |
| Network | 링크가 올라온 NIC(lo 제외)의 IP, RX/TX 속도, **NIC별 RX/TX 그래프** |
| CasparCG Process | PID, 가동 시간, CPU(CEF 포함), RSS/PSS, CEF helper 수, 스레드, AMCP 클라이언트 목록, 힙(RssAnon) 감시 |
| Channels (OSC) | 채널별 consumer, 레이어별 producer·소스·재생 시간·loop/paused |
| AMCP Console | AMCP 명령 한 줄 입력·응답 표시 (↑/↓ 기록) |
| Preview | mediamtx WebRTC 미리보기 (체크박스로 켜고 끔) |
| CasparCG Log | 최신 로그 파일 마지막 1000줄, 레벨 필터, follow |

그래프 범위는 헤더의 **Graphs** 메뉴(5/15/30/60분)에서 고른다. 기록은 백엔드 메모리에 보관되므로
페이지를 새로고침해도 이어지고, 모니터를 재시작하면 비워진다.

## 파일

| 파일 | 설명 |
|---|---|
| `monitor.py` | 백엔드 (HTTP 서버, 샘플러, AMCP/OSC 연결) |
| `static/index.html` | 웹 UI. 요청마다 새로 읽으므로 **수정 후 재시작 불필요** |
| `config.json` | 설정 |
| `caspar-monitor.service` | systemd 유닛 |
| `50-casparcg-monitor.rules` | polkit 규칙 (Start/Stop 버튼용) |

같은 상위 폴더(`/home/sendust/casparcg`)의 관련 스크립트:

| 파일 | 설명 |
|---|---|
| `install-casparcg-service.sh` | CasparCG를 `casparcg.service`로 등록 (`--uninstall`로 제거) |
| `disable-auto-updates.sh` | 자동 업데이트 전체 차단, 송출 관련 패키지 hold (`--undo`로 복구) |

## 설치

```bash
# 1) CasparCG를 systemd 서비스로 등록 (이미 했다면 생략)
sudo /home/sendust/casparcg/install-casparcg-service.sh

# 2) 모니터 서비스 등록
sudo cp caspar-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now caspar-monitor

# 3) Start/Stop service 버튼 허용 (polkit)
sudo cp 50-casparcg-monitor.rules /etc/polkit-1/rules.d/
```

- 모니터는 CasparCG와 **같은 사용자(sendust)**로 실행해야 한다. Kill 버튼이 프로세스에 신호를 보내기 때문이다.
- 유닛에 `NoNewPrivileges=yes`가 있어 sudo를 쓸 수 없으므로, 서비스 제어는 polkit 규칙으로 허용한다.
  규칙은 사용자 `sendust`에게 `casparcg.service`의 start/stop/restart만 허용한다.
- `monitor.py`나 `config.json`을 바꾼 뒤에는 `sudo systemctl restart caspar-monitor`.

## 설정 (`config.json`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `http.bind` / `http.port` | `0.0.0.0` / `8070` | 웹 서버 주소 |
| `sample_interval` | `1.0` | 샘플링 주기(초) |
| `history_minutes` | `60` | CPU/GPU/NIC 그래프 기록 보관 시간(분) |
| `casparcg.process_name` | `casparcg-server-2.5` | 감시할 프로세스 이름 |
| `casparcg.service_name` | `casparcg` | systemd 유닛 이름 (Start/Stop 버튼) |
| `casparcg.host` / `amcp_port` | `127.0.0.1` / `5250` | AMCP 접속 주소 |
| `casparcg.osc_port` | `6250` | OSC 수신 UDP 포트 (casparcg.config의 OSC `default-port`와 같아야 함) |
| `casparcg.osc_stale_seconds` | `1.5` | 이 시간 동안 OSC가 없으면 레이어 정보를 지움 |
| `casparcg.log_dir` / `log_lines` | `/var/log/casparcg` / `1000` | 로그 폴더, 표시 줄 수 |
| `casparcg.kill_timeout` | `5` | Kill: SIGTERM 후 SIGKILL까지 대기(초) |
| `preview.webrtc_port` / `path` | `8889` / `sdi1_va12` | mediamtx WebRTC 미리보기 |
| `memory_watch.*` | 아래 참고 | CasparCG 힙(RssAnon) 감시 |

`memory_watch`: CEF 142는 glibc 힙이 2 GiB를 넘으면 SIGILL로 죽는다(2026-11-10 크래시, 당시 RssAnon 1.69 GiB).
`warn_bytes`(1.25 GiB)·`crit_bytes`(1.50 GiB)를 넘으면 헤더와 프로세스 카드에 경고가 뜨고,
`trend_hours` 동안의 증가 추세로 도달 예상 시간을 표시한다(30분 이상 데이터 필요).

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/status` | 전체 상태 JSON. `?hist_since=<t>`를 붙이면 그 이후 그래프 기록도 포함 |
| GET | `/api/history?since=<t>` | 그래프 기록 `[t, cpu%, [[gpu%, enc%, dec%]...], {nic: [rx, tx]}]` |
| GET | `/api/log?key=<k>` | 로그 마지막 N줄 (변화 없으면 `{"unchanged": true}`) |
| POST | `/api/amcp` | `{"command": "PLAY 1-1 nds LOOP"}` → AMCP 응답 |
| POST | `/api/kill` | `{"confirm": true}` 필요. CasparCG 종료 (systemd가 3초 후 재시작) |
| POST | `/api/service` | `{"action": "start"\|"stop"\|"restart", "confirm": true}` |

```bash
curl -s localhost:8070/api/status | python3 -m json.tool | head
curl -s -X POST localhost:8070/api/amcp -d '{"command":"VERSION"}'
```

## 주의

- **인증이 없다.** 페이지에 접속할 수 있는 사람은 누구나 Kill, Stop, 모든 AMCP 명령(`KILL` 포함)을 실행할 수 있다.
  사내망·Tailscale 등 신뢰할 수 있는 망에서만 열어 둘 것.
- AMCP Console은 명령마다 새 TCP 연결을 연다. CasparCG 로그에 연결/해제 줄이 남고 클라이언트 수가 잠깐 늘어난다.
- `CLS`/`TLS`는 media scanner(설정의 `<media-server>`, localhost:8000)가 없으면 `501 FAILED`.
- Kill은 `Restart=always`라서 곧바로 재시작된다. 완전히 멈추려면 **Stop service**.
- `casparcg.service`는 `KillMode=mixed`로 메인 프로세스에만 SIGTERM을 보낸다
  (기본값이면 CEF helper가 먼저 죽어 "GPU process isn't usable" 코어 덤프가 난다).

## 서버 운영 메모

- **stack smashing detected**: CEF 업데이트/GCM 하위 프로세스가 CasparCG `main()`으로 되돌아오며 생긴다.
  `/etc/hosts`에서 Google 업데이트 도메인 7개를 `0.0.0.0`/`::`로 막아 회피했다.
  로그의 `Failed to connect to MCS endpoint`, `Registration URL fetching failed`는 차단 결과이므로 무시해도 된다.
- **자동 재시작 방지**: unattended-upgrades → needrestart가 CasparCG를 재시작한 적이 있다(2026-11-05 06:39).
  `/etc/needrestart/conf.d/90-casparcg.conf`로 자동 재시작을 막고, `disable-auto-updates.sh`로 자동 업데이트를 껐다.
  보안 패치는 송출이 없는 시간에 직접 `apt upgrade` 후 재시작할 것.

## 문제 해결

```bash
journalctl -u caspar-monitor -f          # 모니터 로그 (Kill/Stop/AMCP 요청 기록 포함)
journalctl -u casparcg -n 100            # CasparCG 서비스 로그
systemctl status caspar-monitor casparcg
```

| 증상 | 확인 |
|---|---|
| 페이지가 안 열림 | `systemctl status caspar-monitor`, 방화벽, 포트 충돌 |
| OSC idle / Channels 비어 있음 | casparcg.config의 OSC 설정, AMCP 연결 여부(OSC는 AMCP 클라이언트로 전송됨) |
| Start/Stop 버튼 거부 | polkit 규칙 설치 여부 (`/etc/polkit-1/rules.d/50-casparcg-monitor.rules`) |
| GPU 카드 N/A | `nvidia-smi` 동작 여부 |
| Preview 안 나옴 | mediamtx 실행 여부, 경로(`sdi1_va12`)에 스트림 존재 여부 |
