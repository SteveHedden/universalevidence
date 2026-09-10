# Search API reference

Use `GET /query/v2` to search across the four supported registries.
For interactive examples and parameter definitions, see the [API documentation](https://api.universalevidence.com/docs).

## Search filters

Supply at least one of `state`, `condition`, `intervention`, `outcome`, or `region`.
Each value must be a full UE vocabulary concept URI. Repeat a parameter to select several concepts.

- `state` matches a concept appearing as either a condition or an outcome topic.
- `condition` and `outcome` restrict matching to the specified role.
- Broader concepts include their descendants.
- Values within a filter use OR by default. Set its corresponding logic parameter, such as `state_logic=and`, to require every selected value.
- Different filters combine with AND.

For example:

```http
GET /query/v2?state=https%3A%2F%2Funiversalevidence.com%2Fvocab%2Fstates%2FMalaria
```

Omit `region` to search anywhere. Geographic matching depends on verified study locations and source coverage; a location elsewhere in a country does not satisfy a selected subnational region.

## Reading results

Responses contain `results` and `meta`. Check the metadata before treating a response as complete:

- `returned_unique_studies`: the number of distinct source-qualified studies returned. A study may appear in multiple presentation rows.
- `sources`: availability and coverage for each registry.
- `truncated` and `approximate`: whether limits or incomplete coverage affect the response.
- `execution_status`: `complete`, `partial`, or `timeout`.
- `completed_sources` and `timed_out_sources`: which sources finished and which exhausted their execution budget.

The default server response budget is 10 seconds. Completed results can be returned when another source times out. An empty incomplete response does not mean that no relevant research exists.

Search limits each upstream source request/branch to 100 unique studies before expanding presentation rows. A combined response can contain more than 100 studies. Study registrations from different registries may describe the same underlying study.

The hosted service is intended for interactive use. Avoid bursts and uncontrolled parallel requests. Rate limiting is enforced by the hosting infrastructure; limits may change.

## Machine-readable definitions

- [Request schema](request.schema.json)
- [Response schema](response.schema.json)
- [Source capabilities](source-capabilities.json)

Graph endpoints use a separate response format. The older `/query` endpoint also differs from `/query/v2`.
