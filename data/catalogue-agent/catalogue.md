# Service catalogue

This catalogue is the source of truth for who owns a service, how an alert on it
escalates, and whether it pages outside working hours. The policy sections apply to
every service and beat an individual entry wherever they say so.

## Escalation ladder

Every alert has an escalation code. The code selects the ladder: the owning team is paged first, the tier duty engineer after the acknowledgement window has passed, and the incident commander after a further thirty minutes with no acknowledgement. An alert that is acknowledged but not resolved does not climb the ladder; it stays with the owning team.

## Change freeze

A change freeze is declared before a release train and lifted after it. While one is in effect, every service in the platform tier pages outside working hours regardless of its own entry, because a freeze means nobody is standing by to pick up a ticket in the morning. Services in other tiers keep the paging rule written in their own entry. A freeze changes nothing about escalation codes, owning teams or acknowledgement windows.

## Incident folding

An alert that names a service already listed on an open incident is folded into that incident and does not page again, whatever its own entry says. Folding applies for as long as the incident is open and stops the moment it is resolved. A folded alert is still recorded against the owning team.

## Working hours

Working hours are 09:00 to 18:00 local time for the owning team, Monday to Friday, excluding public holidays in the team's own location. Anything else is outside working hours. An alert raised at 17:59 is inside working hours; one raised at 18:01 is not.

## service: audit-gateway

- Owning team: payments
- Escalation code: E10
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: us-west-2, us-east-1
- Depends on: notification-broker, search-gateway
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/audit-gateway-overview

## service: audit-validator

- Owning team: checkout
- Escalation code: E17
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: eu-west-1, us-east-1
- Depends on: pricing-router, refund-validator
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/audit-validator-overview

## service: audit-writer

- Owning team: fulfilment
- Escalation code: E24
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: ap-southeast-2, us-west-2
- Depends on: catalogue-sweeper, search-resolver
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/audit-writer-overview

## service: billing-collector

- Owning team: identity
- Escalation code: E31
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: eu-west-1, us-west-2
- Depends on: refund-gateway, search-resolver
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/billing-collector-overview

## service: billing-router

- Owning team: platform
- Escalation code: E38
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: us-east-1, us-west-2
- Depends on: refund-validator, payments-gateway
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/billing-router-overview

## service: billing-sweeper

- Owning team: data
- Escalation code: E45
- Service tier: internal
- Pages outside working hours: yes
- Acknowledge within: 240 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: refund-broker, catalogue-reconciler
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/billing-sweeper-overview

## service: catalogue-dispatcher

- Owning team: growth
- Escalation code: E52
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: eu-west-1, us-east-1
- Depends on: identity-collector, session-scorer
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/catalogue-dispatcher-overview

## service: catalogue-reconciler

- Owning team: support-tooling
- Escalation code: E59
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: eu-west-1, ap-southeast-2
- Depends on: checkout-writer, payments-dispatcher
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/catalogue-reconciler-overview

## service: catalogue-sweeper

- Owning team: payments
- Escalation code: E66
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: billing-collector, invoice-scorer
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/catalogue-sweeper-overview

## service: checkout-dispatcher

- Owning team: checkout
- Escalation code: E73
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: us-west-2, us-east-1
- Depends on: ledger-resolver, pricing-router
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/checkout-dispatcher-overview

## service: checkout-router

- Owning team: fulfilment
- Escalation code: E80
- Service tier: product
- Pages outside working hours: yes
- Acknowledge within: 120 minutes
- Runs in: us-west-2, us-east-1
- Depends on: fraud-reconciler, notification-validator
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/checkout-router-overview

## service: checkout-writer

- Owning team: identity
- Escalation code: E87
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: us-west-2, eu-west-1
- Depends on: session-reconciler, shipment-sweeper
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/checkout-writer-overview

## service: fraud-broker

- Owning team: platform
- Escalation code: E14
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: us-west-2, us-east-1
- Depends on: notification-broker, checkout-writer
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/fraud-broker-overview

## service: fraud-reconciler

- Owning team: data
- Escalation code: E21
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: ap-southeast-2, eu-west-1
- Depends on: shipment-indexer, checkout-writer
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/fraud-reconciler-overview

## service: fraud-validator

- Owning team: growth
- Escalation code: E28
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: eu-west-1, us-west-2
- Depends on: refund-broker, payments-gateway
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/fraud-validator-overview

## service: identity-collector

- Owning team: support-tooling
- Escalation code: E35
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: us-east-1, us-west-2
- Depends on: fraud-broker, invoice-router
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/identity-collector-overview

## service: identity-dispatcher

- Owning team: payments
- Escalation code: E42
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: catalogue-reconciler, ledger-sweeper
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/identity-dispatcher-overview

## service: identity-validator

- Owning team: checkout
- Escalation code: E49
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: us-east-1, us-west-2
- Depends on: notification-validator, billing-collector
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/identity-validator-overview

## service: inventory-dispatcher

- Owning team: fulfilment
- Escalation code: E56
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: ap-southeast-2, us-west-2
- Depends on: session-scorer, ledger-sweeper
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/inventory-dispatcher-overview

## service: inventory-indexer

- Owning team: identity
- Escalation code: E63
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: us-west-2, ap-southeast-2
- Depends on: payments-collector, billing-sweeper
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/inventory-indexer-overview

## service: inventory-sweeper

- Owning team: platform
- Escalation code: E70
- Service tier: internal
- Pages outside working hours: yes
- Acknowledge within: 30 minutes
- Runs in: us-west-2, us-east-1
- Depends on: shipment-indexer, payments-dispatcher
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/inventory-sweeper-overview

## service: invoice-collector

- Owning team: data
- Escalation code: E77
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: eu-west-1, ap-southeast-2
- Depends on: shipment-sweeper, billing-sweeper
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/invoice-collector-overview

## service: invoice-router

- Owning team: growth
- Escalation code: E84
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: shipment-sweeper, invoice-scorer
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/invoice-router-overview

## service: invoice-scorer

- Owning team: support-tooling
- Escalation code: E11
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: us-west-2, eu-west-1
- Depends on: identity-collector, payments-dispatcher
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/invoice-scorer-overview

## service: ledger-resolver

- Owning team: payments
- Escalation code: E18
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: eu-west-1, us-west-2
- Depends on: shipment-broker, fraud-validator
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/ledger-resolver-overview

## service: ledger-scorer

- Owning team: checkout
- Escalation code: E25
- Service tier: product
- Pages outside working hours: yes
- Acknowledge within: 15 minutes
- Runs in: eu-west-1, us-west-2
- Depends on: catalogue-sweeper, catalogue-dispatcher
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/ledger-scorer-overview

## service: ledger-sweeper

- Owning team: fulfilment
- Escalation code: E32
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: us-east-1, us-west-2
- Depends on: session-scorer, audit-writer
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/ledger-sweeper-overview

## service: notification-broker

- Owning team: identity
- Escalation code: E39
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: ap-southeast-2, eu-west-1
- Depends on: inventory-dispatcher, search-validator
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/notification-broker-overview

## service: notification-scorer

- Owning team: platform
- Escalation code: E46
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: ap-southeast-2, eu-west-1
- Depends on: inventory-indexer, audit-gateway
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/notification-scorer-overview

## service: notification-validator

- Owning team: data
- Escalation code: E53
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: eu-west-1, us-west-2
- Depends on: search-resolver, search-validator
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/notification-validator-overview

## service: payments-collector

- Owning team: growth
- Escalation code: E60
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: us-west-2, us-east-1
- Depends on: payments-dispatcher, search-resolver
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/payments-collector-overview

## service: payments-dispatcher

- Owning team: support-tooling
- Escalation code: E67
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: ap-southeast-2, eu-west-1
- Depends on: payments-collector, identity-collector
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/payments-dispatcher-overview

## service: payments-gateway

- Owning team: payments
- Escalation code: E74
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: us-east-1, us-west-2
- Depends on: fraud-validator, search-resolver
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/payments-gateway-overview

## service: pricing-broker

- Owning team: checkout
- Escalation code: E81
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: us-east-1, ap-southeast-2
- Depends on: audit-validator, shipment-broker
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/pricing-broker-overview

## service: pricing-reconciler

- Owning team: fulfilment
- Escalation code: E88
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: us-west-2, ap-southeast-2
- Depends on: invoice-scorer, ledger-sweeper
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/pricing-reconciler-overview

## service: pricing-router

- Owning team: identity
- Escalation code: E15
- Service tier: internal
- Pages outside working hours: yes
- Acknowledge within: 240 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: invoice-scorer, catalogue-sweeper
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/pricing-router-overview

## service: refund-broker

- Owning team: platform
- Escalation code: E22
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: us-west-2, us-east-1
- Depends on: catalogue-reconciler, identity-validator
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/refund-broker-overview

## service: refund-gateway

- Owning team: data
- Escalation code: E29
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: us-east-1, us-west-2
- Depends on: session-scorer, billing-sweeper
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/refund-gateway-overview

## service: refund-validator

- Owning team: growth
- Escalation code: E36
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: eu-west-1, us-east-1
- Depends on: payments-collector, ledger-resolver
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/refund-validator-overview

## service: search-gateway

- Owning team: support-tooling
- Escalation code: E43
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: us-west-2, ap-southeast-2
- Depends on: shipment-broker, audit-writer
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/search-gateway-overview

## service: search-resolver

- Owning team: payments
- Escalation code: E50
- Service tier: product
- Pages outside working hours: yes
- Acknowledge within: 120 minutes
- Runs in: eu-west-1, ap-southeast-2
- Depends on: fraud-broker, refund-validator
- Known failure mode: connection pool exhaustion under retry storms.
- Dashboard: grafana/search-resolver-overview

## service: search-validator

- Owning team: checkout
- Escalation code: E57
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: eu-west-1, us-east-1
- Depends on: catalogue-dispatcher, payments-gateway
- Known failure mode: a slow downstream turning into unbounded queue growth.
- Dashboard: grafana/search-validator-overview

## service: session-reconciler

- Owning team: fulfilment
- Escalation code: E64
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 10 minutes
- Runs in: us-west-2, ap-southeast-2
- Depends on: inventory-sweeper, payments-gateway
- Known failure mode: cache stampede after a deploy invalidates every key at once.
- Dashboard: grafana/session-reconciler-overview

## service: session-scorer

- Owning team: identity
- Escalation code: E71
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 15 minutes
- Runs in: us-east-1, ap-southeast-2
- Depends on: ledger-sweeper, invoice-scorer
- Known failure mode: clock skew rejecting otherwise valid tokens.
- Dashboard: grafana/session-scorer-overview

## service: session-sweeper

- Owning team: platform
- Escalation code: E78
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 30 minutes
- Runs in: ap-southeast-2, eu-west-1
- Depends on: refund-validator, pricing-broker
- Known failure mode: a partial region failover leaving writes split across two primaries.
- Dashboard: grafana/session-sweeper-overview

## service: shipment-broker

- Owning team: data
- Escalation code: E85
- Service tier: platform
- Pages outside working hours: yes
- Acknowledge within: 60 minutes
- Runs in: us-east-1, eu-west-1
- Depends on: session-sweeper, identity-dispatcher
- Known failure mode: back-pressure arriving as timeouts rather than as refusals.
- Dashboard: grafana/shipment-broker-overview

## service: shipment-indexer

- Owning team: growth
- Escalation code: E12
- Service tier: product
- Pages outside working hours: no
- Acknowledge within: 120 minutes
- Runs in: us-west-2, ap-southeast-2
- Depends on: fraud-reconciler, shipment-broker
- Known failure mode: schema drift between the writer and the reader.
- Dashboard: grafana/shipment-indexer-overview

## service: shipment-sweeper

- Owning team: support-tooling
- Escalation code: E19
- Service tier: internal
- Pages outside working hours: no
- Acknowledge within: 240 minutes
- Runs in: ap-southeast-2, us-east-1
- Depends on: shipment-indexer, invoice-scorer
- Known failure mode: a hot partition serialising what should be parallel work.
- Dashboard: grafana/shipment-sweeper-overview
