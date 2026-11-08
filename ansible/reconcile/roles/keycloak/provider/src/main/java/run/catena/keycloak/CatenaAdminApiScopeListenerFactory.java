package run.catena.keycloak;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.RealmModel;
import org.keycloak.models.utils.KeycloakModelUtils;
import org.keycloak.models.utils.PostMigrationEvent;

/**
 * Registers the admin API scope grant as a global admin-event listener. The option clients
 * names the clients, as a comma list of realm/clientId pairs. After the server's migration,
 * every named client of a realm holding the scope gets it.
 */
public class CatenaAdminApiScopeListenerFactory implements EventListenerProviderFactory {

    public static final String PROVIDER_ID = "catena-admin-api-scope";
    static final String CLIENTS = "clients";

    private static final Logger LOG = Logger.getLogger(CatenaAdminApiScopeListenerFactory.class);

    private Map<String, Set<String>> clients = Collections.emptyMap();

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public boolean isGlobal() {
        return true;
    }

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new CatenaAdminApiScopeListener(session, clients, LOG);
    }

    @Override
    public void init(Config.Scope config) {
        clients = parse(config.get(CLIENTS));
    }

    /** The realm/clientId pairs of a comma list by realm; an entry without both parts is skipped. */
    static Map<String, Set<String>> parse(String value) {
        Map<String, Set<String>> byRealm = new LinkedHashMap<>();
        if (value == null) {
            return byRealm;
        }
        for (String entry : value.split(",")) {
            String pair = entry.trim();
            int slash = pair.indexOf('/');
            if (slash <= 0 || slash == pair.length() - 1) {
                continue;
            }
            byRealm.computeIfAbsent(pair.substring(0, slash), r -> new LinkedHashSet<>())
                    .add(pair.substring(slash + 1));
        }
        return byRealm;
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        factory.register(event -> {
            if (event instanceof PostMigrationEvent) {
                sweep(((PostMigrationEvent) event).getFactory(), KeycloakModelUtils::runJobInTransaction, clients);
            }
        });
    }

    static void sweep(KeycloakSessionFactory factory, CatenaTierBindingListenerFactory.Transactions transactions,
            Map<String, Set<String>> clients) {
        for (Map.Entry<String, Set<String>> entry : clients.entrySet()) {
            try {
                transactions.run(factory, session -> {
                    RealmModel realm = session.realms().getRealmByName(entry.getKey());
                    if (realm != null) {
                        CatenaAdminApiScopeListener.grant(realm, entry.getValue(), LOG);
                    }
                });
            } catch (Throwable t) {
                LOG.error("Could not give the admin API scope to the clients of a realm", t);
            }
        }
    }

    @Override
    public void close() {
    }
}
