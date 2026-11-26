# Add a second SAP system

Terms are defined in [Concepts](../../docs/start/concepts.md).

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP.

Each entry under `layers.sap.systems` is one SAP system. It has its own AWS for SAP MCP (Model
Context Protocol) Server, its own Gateway target `sap-<system>` and its own secret. This page adds
a second system to a deployment that already has one. The settings themselves are in the [walkthrough](walkthrough.md#enable).

**Two systems in one deployment, or one deployment per system?** Development, quality and
production SAP systems usually get one deployment each. Each use case's own pages say how its
work moves between them ([Use cases](../../use-cases/README.md)).
Put two systems in one deployment when the same people work on both. An example is two
production systems for two regions or two company groups.

## 1. Prepare the SAP side

The new system needs the same things as the first one
([SAP setup: prerequisite decisions](sap-setup.md#prerequisite-decisions)):

- a service user and its roles
- the services its use cases use
- an HTTPS certificate from a publicly trusted certificate authority (CA)

Check two things, because the roles differ for each
([SAP Gateway deployment and OData version](sap-setup.md#sap-gateway-deployment-and-odata-version)):

- whether the new system runs an embedded SAP Gateway or sits behind an SAP Gateway hub
- whether its services are OData (Open Data Protocol) V2 or V4

Activate the services in this system too. Activating them in the first system does not activate
them here ([moving services](sap-setup.md#moving-services-between-systems)). Give the new system
its own service user, because each system has its own secret.

## 2. Add the system

```yaml
layers:
  sap:
    systems:
      s4: { ... }                         # the existing system, unchanged
      s4-emea:                            # at most 16 characters; becomes sap-s4-emea
        system_role: production
        base_url: https://<emea-sap-host>/sap/opu/odata/sap/
        client: "200"
        secret_name: <project>-<env>/sap/s4-emea
```

Then store its credentials:

```bash
./scripts/deploy.sh layer run sap secrets s4-emea
```

## 3. Give the use case access

The use case's access row under `layers.sap.access` limits what it may do on each system:

```yaml
    access:
      <name>:                                             # the use case
        read: "*"                                         # every system, the new one included
        write: {s4: [function_import], s4-emea: [function_import]}
        bypass_gateway: [s4, s4-emea]                     # the systems the poller reads
```

`read: "*"` already covers the new system. Name its writes under `write` only if the use case
will write there. Add it to `bypass_gateway` only if the use case's own functions call it
directly, such as a poller or a test-data seeder. `bypass_gateway` is the list of those systems.

## 4. Point the use case at the system

Each use case decides which system each of its calls goes to. Its own pages say where it names
the system ([Use cases](../../use-cases/README.md)).

**A use case that polls packs needs a system in every pack.** With two systems, a poller skips a
pack without `sap_system` and logs `Skipping <pack>: its sap_system is not one of [...]`. A pack
without it works only while there is one system.

In each pack's `pack.json`:

- Set `"sap_system"` to the system's key, such as `"s4"`.
- Name that system's tools in `gateway_tools` (`sap-s4-emea___odata_read`, and so on). A pack
  contains only its own system's SAP tools.

To run the same process on both systems, make a second pack. It is a copy with its own
`pack_id`, `sap_system` and tools. Both changes are structure changes. A pack administrator makes
them in a development deployment, or a coding agent makes them. The use case's own pages say
who may change what.

## 5. Deploy and verify

```bash
./scripts/deploy.sh deploy
./scripts/deploy.sh verify
```

The deploy adds the new system's server and target to the SAP layer's stack. It also updates the
use case's SAP access claim and Cedar permits. `verify` checks that each SAP runtime is READY and
verifies SAP's certificate, and that each target is READY ([Verify checks](reference/verify.md)).
Then run one of the use case's own commands on the new system and check the result. For a use
case that polls packs:

```bash
./scripts/deploy.sh usecase run <name> poll <pack_id>
```

## Known limitations

- **Two SAP systems have not run live.** Routing by a pack's `sap_system` is unit-tested only,
  because no test environment has a second SAP system. A pack that names its system gets only
  that system's tools. A run with no `sap_system`, such as a discovery run, gets both systems'
  tools under their full names (`sap-a___odata_read` and `sap-b___odata_read`). On the first
  deploy with a second system, run one pack on each system and check which system each call
  reached.
