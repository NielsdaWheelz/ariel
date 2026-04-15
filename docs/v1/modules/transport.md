# Transport

## Scope

This document covers transport lifecycle ownership.

## Lifecycle

- A transport layer should not control the lifecycle of application work unless that work genuinely depends on the transport being alive.
- If application work must survive transport reconnects, decouple it into a scope that outlives the transport.
- Treat the transport layer as a delivery pipe that can disconnect and reconnect without interrupting in-flight application work.
