package run.catena.keycloak;

import java.util.ArrayList;
import java.util.List;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.KeycloakSessionTask;
import org.keycloak.models.utils.KeycloakModelUtils;
import org.keycloak.models.utils.PostMigrationEvent;

/**
 * Registers the post-broker-login binding as a global admin-event listener.
 * The option exemptClient names the client whose identity provider updates
 * are left as they are. After the server's migration, every realm holding
 * the flow gets it on the providers that name none.
 */
public class CatenaTierBindingListenerFactory implements EventListenerProviderFactory {

    public static final String PROVIDER_ID = "catena-tier-binding";
    static final String EXEMPT_CLIENT = "exemptClient";

    private static final Logger LOG = Logger.getLogger(CatenaTierBindingListenerFactory.class);

    private String exemptClient = "";

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
        return new CatenaTierBindingListener(session, exemptClient, Config.getAdminRealm(), LOG);
    }

    @Override
    public void init(Config.Scope config) {
        String value = config.get(EXEMPT_CLIENT);
        exemptClient = value == null ? "" : value;
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        factory.register(event -> {
            if (event instanceof PostMigrationEvent) {
                sweep(((PostMigrationEvent) event).getFactory(), KeycloakModelUtils::runJobInTransaction);
            }
        });
    }

    /** Runs a task in its own transaction on a session of the factory. */
    interface Transactions {
        void run(KeycloakSessionFactory factory, KeycloakSessionTask task);
    }

    static void sweep(KeycloakSessionFactory factory, Transactions transactions) {
        try {
            List<String> realmIds = new ArrayList<>();
            transactions.run(factory,
                    session -> session.realms().getRealmsStream().forEach(r -> realmIds.add(r.getId())));
            for (String realmId : realmIds) {
                try {
                    transactions.run(factory,
                            session -> CatenaTierBindingListener.bindUnbound(session, realmId));
                } catch (Throwable t) {
                    LOG.error("Could not bind the post-broker-login flow in a realm", t);
                }
            }
        } catch (Throwable t) {
            LOG.error("Could not list the realms to bind the post-broker-login flow", t);
        }
    }

    @Override
    public void close() {
    }
}
