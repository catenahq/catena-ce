# Catena Keycloak extension

One JAR, `catena-keycloak.jar`, compiled against the Keycloak version in `pom.xml` (`keycloak.version`, which follows `keycloak_image_tag` in `catena-ce/ansible/reconcile/roles/keycloak/defaults/main.yml`) on Java 21. It holds two providers.

- `catena-tier-refusal`: an authenticator that refuses a sign-in like Keycloak's Deny access step, unless the user is a direct member of a top-level group named in the comma list in the signing-in client's attribute `catena.access.groups`, which admits the sign-in (a group of that name under another group does not count). Its factory names the reference category `catena-tier`, outside the ones the brute-force guard counts (`password`, `otp`, `recovery-authn-codes`), so a refusal on tier does not lock the account. It takes no configuration and offers the requirements REQUIRED and DISABLED.
- `catena-tier-binding`: a global admin-event listener. When an identity provider is created or updated, including by Partial import, and names no post-broker-login flow, it names the flow `catena-post-broker-login` of the realm, keeping the provider's organization links. The server option `--spi-events-listener--catena-tier-binding--exempt-client=<clientId>` names a client of the admin realm whose updates are left alone. After the server's migration, the same binding runs over every realm holding the flow.

Both register through `src/main/resources/META-INF/services`.

## Build and test

Maven resolves Keycloak as `provided` dependencies, so the JAR carries only this code.

    mvn -B -ntp --strict-checksums verify

runs the JUnit 5 and Mockito tests under `src/test/java` and writes `target/catena-keycloak.jar`. The build is reproducible: `project.build.outputTimestamp` is fixed, so two builds give the same SHA-256. The CI job `keycloak-provider` builds twice and compares.
