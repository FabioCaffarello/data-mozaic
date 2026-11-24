# BotBuild manifest draft/v2 (BC15)

Status: **draft / illustrative**, owner BC15. This manifest is an immutable build identity and compatibility declaration. Its digests are not a signature, publisher identity, execution authorization, or BotRelease approval.

Version 2 adds logical bot/source identity, backend profile identity, dependency identity, and an explicit worker compatibility declaration including the target platform. The compiler accepts an optional digest of the original Playbook bytes supplied by its caller; it never reconstructs that digest from IR. A missing digest is represented as `null`. The host toolchain and intended target are separate fields: the initial Scrapy image accepts only Python 3.12, Scrapy 2.20.0 and `linux/amd64`.

The structural integrity implementation is `mozaic-bot-build`, a stdlib-only library consumed by BC15 and the Scrapy Collector. Consumers select supported manifest versions explicitly. The worker requires v2; standalone BC15 consumers continue to verify legacy v1 builds. There is no silent v1-to-v2 migration.
