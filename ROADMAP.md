# Possible next directions

The v1.0.0-beta.2 baseline is complete. No new feature is currently sequenced.
These are discussion topics, not requirements, architectural prohibitions or promises.

- **Portfolio factsheet:** credit quality, duration and underlying asset mix across
  the portfolio, using manually maintained dated inputs where useful. First define
  the questions, aggregation rules and coverage of unknown/stale exposures.
- **Optional bucket planning:** portfolio analysis and retirement strategies that
  can support a simple allocation/rebalancing portfolio without requiring spending
  buckets. Agree the calculation/withdrawal behavior as well as page visibility.
- **Securities data:** consider the benefit and maintenance cost of a catalogue,
  price updates and factsheet inputs. Provider choice, licensing and any new network
  behavior are unresolved; no feed, scraping or paid subscription is authorized.
- **Public feedback release:** review source packaging, setup instructions and
  contributor guidance for a release under the BSD 3-Clause license.

Before implementing a feature, agree its user outcome and a bounded usable first
slice. Revise current design choices where warranted; retain data compatibility and
financial integrity. Record completed changes in release notes rather than growing
this file into a transcript of decisions.
