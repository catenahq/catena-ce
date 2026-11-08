package run.catena.keycloak;

import java.util.Map;
import java.util.Set;

import org.jboss.logging.Logger;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.OperationType;
import org.keycloak.events.admin.ResourceType;
import org.keycloak.models.ClientModel;
import org.keycloak.models.ClientScopeModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;

/**
 * Gives the named clients of a realm the client scope kc-admin-api as a default scope when
 * a client of that realm is created or updated, including by Partial import. The scope adds
 * the audience and the scope the Admin REST API asks of a token; it grants no role. A realm
 * without the scope is left alone, and so is a client holding it as a default or optional
 * scope.
 */
public class CatenaAdminApiScopeListener implements EventListenerProvider {

    /** The client scope Keycloak 26.8.2 and later create in every realm for the Admin REST API. */
    static final String SCOPE = "kc-admin-api";

    /** Path prefix of the events Partial import raises for each client it writes. */
    static final String PARTIAL_IMPORT_PATH = "clients/";

    private final KeycloakSession session;
    private final Map<String, Set<String>> clients;
    private final Logger log;

    /** clients maps a realm name to the client ids given the scope there. */
    CatenaAdminApiScopeListener(KeycloakSession session, Map<String, Set<String>> clients, Logger log) {
        this.session = session;
        this.clients = clients;
        this.log = log;
    }

    @Override
    public void onEvent(Event event) {
    }

    @Override
    public void onEvent(AdminEvent event, boolean includeRepresentation) {
        try {
            if (!isClientWrite(event)) {
                return;
            }
            RealmModel realm = session.realms().getRealm(event.getRealmId());
            if (realm != null) {
                grant(realm, clients.get(realm.getName()), log);
            }
        } catch (Throwable t) {
            log.error("Could not give the admin API scope to a client", t);
        }
    }

    /** A client created or updated, or the Partial import event of one, which has the realm as its resource. */
    private static boolean isClientWrite(AdminEvent event) {
        OperationType operation = event.getOperationType();
        if (operation != OperationType.CREATE && operation != OperationType.UPDATE) {
            return false;
        }
        if (event.getResourceType() == ResourceType.CLIENT) {
            return true;
        }
        String path = event.getResourcePath();
        return event.getResourceType() == ResourceType.REALM && path != null
                && path.startsWith(PARTIAL_IMPORT_PATH);
    }

    /** Gives the realm's scope as a default scope to each client named that holds it in neither list. */
    static void grant(RealmModel realm, Set<String> clientIds, Logger log) {
        if (clientIds == null || clientIds.isEmpty()) {
            return;
        }
        ClientScopeModel scope = realm.getClientScopesStream()
                .filter(s -> SCOPE.equals(s.getName()))
                .findFirst()
                .orElse(null);
        if (scope == null) {
            return;
        }
        for (String clientId : clientIds) {
            ClientModel client = realm.getClientByClientId(clientId);
            if (client == null || client.getClientScopes(true).containsKey(SCOPE)
                    || client.getClientScopes(false).containsKey(SCOPE)) {
                continue;
            }
            client.addClientScope(scope, true);
            log.infof("Gave the client scope %s to the client %s of the realm %s", SCOPE, clientId, realm.getName());
        }
    }

    @Override
    public void close() {
    }
}
