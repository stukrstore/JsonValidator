# DocumentDB `$jsonSchema` reference

Source: https://learn.microsoft.com/en-us/documentdb/query/operators/evaluation-query/$jsonschema

The `$jsonSchema` operator validates documents against a JSON Schema specification.
It ensures that documents conform to a predefined structure, data types, and
validation rules.

## Syntax

```js
db.createCollection("collectionName", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["field1", "field2"],
      properties: {
        field1: { bsonType: "string", description: "..." },
        field2: { bsonType: "int", minimum: 0 }
      }
    }
  },
  validationLevel: "strict",   // "strict" | "moderate"
  validationAction: "error"    // "error"  | "warn"
})
```

## Parameters

- `bsonType` — BSON types the field must match (`"string"`, `"int"`, `"double"`,
  `"object"`, `"array"`, `"bool"`, `"date"`, `"null"`, ...).
- `properties` — validation rules for specific fields.
- `minimum` / `maximum` — numeric constraints.
- `minLength` / `maxLength` — string length constraints.
- `minItems` / `maxItems` — array length constraints.
- `pattern` — regex for strings.
- `items` — schema for array elements.
- `uniqueItems` — boolean.

## Supported keywords

`additionalItems`, `bsonType`, `exclusiveMinimum`, `exclusiveMaximum`, `items`,
`minimum`, `maximum`, `minItems`, `maxItems`, `multipleOf`, `minLength`,
`maxLength`, `pattern`, `properties`, `required`, `type`, `uniqueItems`.

## Unsupported keywords (and workarounds)

| Keyword | Workaround |
|---|---|
| `additionalProperties` | Use explicit `properties` definitions |
| `allOf` | Use nested validation |
| `anyOf` | Use separate queries |
| `dependencies` | Handle in application logic |
| `enum` | Use `$in` operator instead |
| `maxProperties` / `minProperties` | Handle in application logic |
| `not` | Use positive validation rules |
| `oneOf` | Use application-level validation |
| `patternProperties` | Use explicit property names |
| `title` | Use `description` instead |

## Examples

### Basic structure validation

```js
db.stores.find({
  $jsonSchema: {
    bsonType: "object",
    properties: {
      _id:  { bsonType: "string" },
      name: { bsonType: "string", minLength: 5, maxLength: 100 },
      location: {
        bsonType: "object",
        properties: {
          lat: { bsonType: "double", minimum: -90,  maximum: 90 },
          lon: { bsonType: "double", minimum: -180, maximum: 180 }
        }
      }
    }
  }
}).limit(1)
```

### Array items validation

```js
db.stores.find({
  $jsonSchema: {
    bsonType: "object",
    properties: {
      sales: {
        bsonType: "object",
        properties: {
          totalSales: { bsonType: "int", minimum: 0 },
          salesByCategory: {
            bsonType: "array",
            minItems: 1,
            items: {
              bsonType: "object",
              properties: {
                categoryName: { bsonType: "string", minLength: 1 },
                totalSales:   { bsonType: "int", minimum: 0 }
              }
            }
          }
        }
      }
    }
  }
}).limit(1)
```

### Combining with query operators

```js
db.stores.find({
  $and: [
    {
      $jsonSchema: {
        properties: {
          staff: {
            bsonType: "object",
            properties: {
              totalStaff: {
                bsonType: "object",
                properties: { fullTime: { bsonType: "int", minimum: 1 } }
              }
            }
          }
        }
      }
    },
    { "sales.totalSales": { $gt: 10000 } }
  ]
}).limit(1)
```
