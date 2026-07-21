# CLAUDE.md

이 프로젝트는 KIS(한국투자증권) Open API를 이용한 주식 조회/거래 도구입니다.

이 문서는 Claude Code가 이 프로젝트에서 반드시 지켜야 할 두 가지 규칙을 정의합니다.

## 1. 패키지 관리는 반드시 `uv`로만

이 프로젝트의 모든 Python 패키지/의존성/가상환경 관리는 `uv`로만 합니다.

- 사용 금지: `pip install`, `poetry`, `conda` 등을 직접 호출하지 마세요.
- 항상 사용: `uv add`, `uv sync`, `uv run`, `uv remove` 등 `uv` 명령을 사용하세요.

## 2. 주가/투자 데이터는 반드시 KIS Open API로만

이 프로젝트에서 주가나 투자 데이터를 조회할 때는 반드시 한국투자증권(KIS) 공식 Open API를 사용합니다.

- 사용 금지: Yahoo Finance, Alpha Vantage 등 범용 서드파티 시세 API는 사용하지 마세요.
- KIS API 관련 코드를 작성하거나 수정하기 전에, 프로젝트에 설정된 MCP 서버(`kis-code-assistant-mcp`, `.mcp.json` 참고)를 통해 최신 엔드포인트, `tr_id` 값, 인증 흐름, 헤더 등 API 스펙을 먼저 확인하세요.
- 예시: `kis_price.py` (`uv run kis_price.py`)는 KIS Open API로 삼성전자(005930) 현재가를 조회하는 참고 구현입니다.
- **예외**: 국내 상장 ETF/ETN 전체 목록처럼 KIS API가 제공하지 않는 데이터에 한해서만, 한국거래소(KRX)가 직접 운영하는 공식 Open API(`openapi.krx.co.kr` / `data.krx.co.kr`, `AUTH_KEY` 인증)를 사용할 수 있습니다. 이 예외는 "종목 리스트/마스터 데이터" 조회 용도로 한정하며, 개별 종목의 시세·주문·잔고 등 KIS API로 가능한 조회에는 계속 KIS API를 사용하세요.
- 예시: `krx_etf_list.py` (`uv run krx_etf_list.py`)는 KRX Open API로 국내 상장 ETF 전체 목록을 조회하는 참고 구현입니다.
