package run.catena.keycloak;

import java.util.List;

import org.jboss.logging.Logger;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.OperationType;
import org.keycloak.events.admin.ResourceType;
import org.keycloak.models.AuthenticationFlowModel;
import org.keycloak.models.ClientModel;
import org.keycloak.models.IdentityProviderModel;
import org.keycloak.models.IdentityProviderQuery;
import org.keycloak.models.IdentityProviderStorageProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;

/**
 * Names the post-broker-login flow on an identity provider that has none,
 * when an administrator creates or updates the provider. The flow is the one
 * the realm attribute catenaPostBrokerLoginFlow names. The model update
 * keeps the provider's organization links and publishes no admin event.
 */
public class CatenaTierBindingListener implements EventListenerProvider {

    /** The realm attribute naming the alias of the flow providers are bound to. */
    static final String FLOW_ATTRIBUTE = "catenaPostBrokerLoginFlow";

    /** Path prefix of the events Partial import raises for each identity provider it writes. */
    static final String PARTIAL_IMPORT_PATH = "identity-provider-settings/";

    private final KeycloakSession session;
    private final String exemptClient;
    private final String adminRealm;
    private final Logger log;

    /** The exempt client lives in the admin realm named adminRealm. */
    CatenaTierBindingListener(KeycloakSession session, String exemptClient, String adminRealm, Logger log) {
        this.session = session;
        this.exemptClient = exemptClient;
        this.adminRealm = adminRealm;
        this.log = log;
    }

    @Override
    public void onEvent(Event event) {
    }

    @Override
    public void onEvent(AdminEvent event, boolean includeRepresentation) {
        try {
            bindFor(event);
        } catch (Throwable t) {
            log.error("Could not name the post-broker-login flow on an identity provider", t);
        }
    }

    private void bindFor(AdminEvent event) {
        if (!isProviderEvent(event)) {
            return;
        }
        OperationType operation = event.getOperationType();
        if (operation != OperationType.CREATE && operation != OperationType.UPDATE) {
            return;
        }
        if (operation == OperationType.UPDATE && byExemptClient(event)) {
            return;
        }
        String key = event.getResourceId();
        RealmModel realm = session.realms().getRealm(event.getRealmId());
        if (key == null || realm == null) {
            return;
        }
        RealmModel previous = session.getContext().getRealm();
        session.getContext().setRealm(realm);
        try {
            if (flowOf(realm) == null) {
                return;
            }
            bind(session, realm, session.identityProviders().getByIdOrAlias(key));
        } finally {
            if (previous != null) {
                session.getContext().setRealm(previous);
            }
        }
    }

    /** The flow the realm attribute names, or null when it names none or the realm holds no such flow. */
    static AuthenticationFlowModel flowOf(RealmModel realm) {
        String alias = realm.getAttribute(FLOW_ATTRIBUTE);
        return alias == null || alias.isBlank() ? null : realm.getFlowByAlias(alias);
    }

    /** An identity provider event, or the Partial import event of one, which has the realm as its resource. */
    private static boolean isProviderEvent(AdminEvent event) {
        if (event.getResourceType() == ResourceType.IDENTITY_PROVIDER) {
            return true;
        }
        String path = event.getResourcePath();
        return event.getResourceType() == ResourceType.REALM && path != null
                && path.startsWith(PARTIAL_IMPORT_PATH);
    }

    /**
     * The event carries the internal ids of the realm and the client that made the change: it is
     * the exempt client's when it was made in the admin realm by the client of that name there.
     */
    private boolean byExemptClient(AdminEvent event) {
        if (exemptClient == null || exemptClient.isEmpty() || event.getAuthDetails() == null) {
            return false;
        }
        RealmModel admin = session.realms().getRealmByName(adminRealm);
        if (admin == null || !admin.getId().equals(event.getAuthDetails().getRealmId())) {
            return false;
        }
        ClientModel client = session.clients().getClientByClientId(admin, exemptClient);
        return client != null && client.getId().equals(event.getAuthDetails().getClientId());
    }

    /**
     * Binds the realm's flow to the provider when it names none; the realm is the session's context
     * realm. The model written is the stored one: the cache layer's copy reports a provider linked
     * to organizations as disabled when the organizations are off, and the update would store that.
     */
    static void bind(KeycloakSession session, RealmModel realm, IdentityProviderModel model) {
        if (model == null) {
            return;
        }
        String current = model.getPostBrokerLoginFlowId();
        if (current != null && !current.isEmpty()) {
            return;
        }
        IdentityProviderModel stored = session.getProvider(IdentityProviderStorageProvider.class, "jpa")
                .getById(model.getInternalId());
        if (stored == null) {
            return;
        }
        stored.setPostBrokerLoginFlowId(flowOf(realm).getId());
        session.identityProviders().update(stored);
    }

    /** Binds every unbound user-authentication provider of the realm when the realm holds the flow. */
    static void bindUnbound(KeycloakSession session, String realmId) {
        RealmModel realm = session.realms().getRealm(realmId);
        if (realm == null || flowOf(realm) == null) {
            return;
        }
        session.getContext().setRealm(realm);
        List<IdentityProviderModel> unbound = session.identityProviders()
                .getAllStream(IdentityProviderQuery.userAuthentication()
                        .with(IdentityProviderModel.POST_BROKER_LOGIN_FLOW_ID, ""), null, null)
                .toList();
        for (IdentityProviderModel model : unbound) {
            bind(session, realm, model);
        }
    }

    @Override
    public void close() {
    }
}
