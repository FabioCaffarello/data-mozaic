# BC05 worker-independent consumer fixture

This example uses `contracts/acquisition-output/draft/v1/fixtures/valid/record-output.json`, which is synthetic test data. It does not claim to be a record observed from Banco Central, CVM, or another real source.

The consumer maps the draft wire document into a private BC05 output value, stages it through the application port, seals and publishes the in-memory OutputSet, then reads the published output through the consumer query. The consumer depends on the output contract/application boundary; it does not import Scrapy, a worker package, MongoDB, PostgreSQL or MinIO.

Run with:

```sh
make acquisition-output-fixture-example
```

Expected result is checked against `expected-consumer-view.json`. This demonstrates the local reference adapter only; it is not persistence or distributed publication evidence.
