# 한국 시장 데이터: 카카오 로그인 없이 아침 배치 실행

아침 `stock_analysis_orchestrator.py --mode morning` 은 KRX Data Marketplace 카카오 로그인(Playwright 2FA)을 기다리지 않습니다. 세션이 만료돼도 `2FA 확인 대기 중` 루프에 들어가지 않고, 수 초 안에 다음 소스로 넘어갑니다.

## 사용하는 소스

기본 순서 (`PRISM_MARKET_DATA_SOURCES` / `PRISM_REPORT_DATA_SOURCES`):

1. **KIS** — 일봉, 지수, 투자자 수급, 장중 추정 수급. 아침 인트라데이의 기본 소스입니다.
2. **FinanceDataReader** — 시세와 지수. 투자자 수급은 없습니다. 호출당 20초를 넘기면 실패로 처리합니다.
3. **Naver** — 최근 약 10영업일 투자자 수급만. 시세 원천이 아닙니다. HTTP 타임아웃은 10초입니다.

`krx` 는 설정에 남아 있어도 건너뜁니다. 그 이름은 `krx_data_client` / `kospi_kosdaq_stock_server` 카카오 로그인입니다.

구형 `mcp_agent.config.yaml` 이 `kospi_kosdaq_stock_server` 를 실행하도록 되어 있어도, 프로세스가 MCP 서버를 띄우기 전에 `cores.market_data.mcp_server` 로 바꿉니다. `KAKAO_ID`, `KAKAO_PW`, `KRX_ID`, `KRX_PW` 는 서버 환경에서 제거합니다. 값은 로그에 남기지 않습니다.

수급을 줄 수 있는 소스가 모두 실패하면 빈 결과와 오류 로그만 남기고 리포트는 계속합니다. 없는 수급을 0으로 채우지 않습니다.

## KRX Open API 와 아침 장중 데이터

[KRX Open API](https://openapi.krx.co.kr) (`openapi.krx.co.kr`) 는 계약된 EOD API 입니다.

- 전 종목 스냅샷은 **전일(이전 세션) 종가** 용도입니다. 당일 `basDd` 는 장중·장 마감 직후에 비어 있는 경우가 있어 **09:30 아침 스크리닝을 대체하지 못합니다.**
- 호출은 날짜 1개당 시장 전체 1회이며, 종목별 기간 조회가 아닙니다.
- 투자자별 매매는 제공하지 않습니다.

그래서 아침 배치는 Open API 를 블로킹 경로에 올리지 않습니다. 키가 없어도 파이프라인은 KIS와 공개 폴백으로 진행합니다. 키를 쓸 경우에만 `.env` 에 두고 커밋하지 마십시오.

```bash
# 선택. 비우면 Open API 를 호출하지 않습니다.
# KRX_OPENAPI_AUTH_KEY=
```

## 확인 방법

네트워크 없이:

```bash
pytest tests/test_kakao_krx_login_disabled.py tests/test_market_data_sources.py tests/test_kis_only_architecture.py -q
```

아침 경로가 카카오를 기다리지 않는지 수동으로 보려면, 카카오 세션 없이 `--no-telegram` 으로 기동하고 로그에 `Kakao/Playwright KRX login disabled` 와 `market data sources: kis -> fdr -> naver` 가 있는지 확인합니다. `2FA 확인 대기 중` 이 나오면 안 됩니다.
