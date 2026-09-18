```console
$ imp log samo71 --summary
default  ·  samo71  ·  merged
                                         time      in     out      cost
threshold
  threshold                            5m 20s    1.4M   27.8k   [$2.45]
  … 4 rows elided …
spec
  spec-finalizer                        2m 8s  278.0k   10.5k   [$0.79]
  spec-verify-commit                       0s       —       —         —
  spec-reviewer                        4m 51s  648.3k   22.6k   [$1.44]
  spec-finalizer                       1m 49s  625.2k    8.7k   [$1.00]
  spec-verify-commit                       0s       —       —         —
  spec-reviewer                        4m 44s  748.3k   22.3k   [$1.48]
plan
  plan-writer                          8m 35s    1.5M   42.4k   [$2.84]
  plan-verify-commit                       0s       —       —         —
  plan-reviewer                        6m 15s    1.5M   29.0k   [$2.24]
  plan-finalized                           0s       —       —         —
implement
  task-lead                           11m 27s    2.0M   48.6k   [$2.99]
    ↳ coder (writing code)                47s  149.6k    2.9k   [$0.19]
    ↳ code-reviewer (reviewing code)    1m 6s   45.7k    4.2k   [$0.13]
    ↳ coder (writing code)             1m 25s  265.0k    6.1k   [$0.29]
    ↳ code-reviewer (reviewing code)   1m 29s   72.5k    5.8k   [$0.20]
    ↳ coder (writing code)              1m 3s  163.9k    3.0k   [$0.19]
    ↳ code-reviewer (reviewing code)   1m 36s  124.8k    6.3k   [$0.23]
  verify                                2m 9s       —       —         —
  check-next-plan                          0s       —       —         —
  … 33 rows elided …
converge
  gap-check                             3m 1s  640.0k   10.6k   [$1.10]
  quest-review                         5m 30s    3.0M   21.4k   [$3.04]
  deferred-check                           0s       —       —         —
  deferred-sweep                       3m 17s  528.7k   12.0k   [$1.01]
  … 5 rows elided …
  converge-verify                       3m 7s       —       —         —
integrate
  wait-for-head                            0s       —       —         —
  integrate-rebase                         0s       —       —         —
  summarizer                              22s   52.5k    1.0k   [$0.12]
  finalize                                53s  175.6k    2.8k   [$0.42]
  cleanup-merge                            0s       —       —         —
  assert-stamped                           0s       —       —         —
  merge-to-base                            0s       —       —         —

  total                                 2h 6m   28.0M  531.1k  [$42.20]
```
