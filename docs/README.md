# 문서

tokmo-v3의 문서 목록입니다. 시스템이 무엇을 하는지와 첫 설치 순서는 [프로젝트 README](../README.md)를 먼저 보시기 바랍니다.

## 설치와 실행

- [설치와 모듈 설정](setup.md): 지원 환경, 설치, 모듈 켜고 끄기, AI 호출 승인(제품 게이트)
- [Docker로 실행](setup-docker.md): Windows를 포함해 컨테이너로 실행하는 방법
- [자격증명 안내](api-keys.md): 모듈별로 필요한 키와 파일 위치, 모델 레지스트리

## 운영

- [운영 안내](operations.md): 스케줄, 실주문 조건, 모델 호출과 쿼터, 데이터 취급, 모듈별 완성도
- [펄스 일시정지 런북](pulse-g2-shadow-runbook.md): 펄스가 호출 한도를 넘어 멈췄을 때 수동으로 푸는 절차

## 구조

- [구조](architecture.md): 계층별 역할, 의존 방향, 데이터 위치
- [제품 모델 게이트](../ops/product-gate/README.md): 승인 파일이 무엇을 묶는지
- [관심 종목 목록](../watchlists/README.md): `watchlists/`에 넣을 수 있는 것과 없는 것
