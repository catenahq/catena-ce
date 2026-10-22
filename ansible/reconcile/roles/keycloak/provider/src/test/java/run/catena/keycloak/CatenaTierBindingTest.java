package run.catena.keycloak;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

import org.jboss.logging.Logger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.AuthDetails;
import org.keycloak.events.admin.OperationType;
import org.keycloak.events.admin.ResourceType;
import org.keycloak.models.AuthenticationFlowModel;
import org.keycloak.models.ClientModel;
import org.keycloak.models.ClientProvider;
import org.keycloak.models.IdentityProviderModel;
import org.keycloak.models.IdentityProviderQuery;
import org.keycloak.models.IdentityProviderStorageProvider;
import org.keycloak.models.KeycloakContext;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RealmProvider;
import org.mockito.ArgumentCaptor;

class CatenaTierBindingTest {

    private static final String FLOW_ID = "flow-id";
    private static final String ADMIN_REALM_ID = "admin-realm-id";

    private KeycloakSession session;
    private RealmProvider realms;
    private ClientProvider clients;
    private IdentityProviderStorageProvider providers;
    private IdentityProviderStorageProvider jpa;
    private KeycloakContext context;
    private RealmModel realm;
    private RealmModel adminRealm;
    private IdentityProviderModel stored;
    private Logger log;
    private CatenaTierBindingListener listener;

    @BeforeEach
    void setUp() {
        session = mock(KeycloakSession.class);
        realms = mock(RealmProvider.class);
        clients = mock(ClientProvider.class);
        providers = mock(IdentityProviderStorageProvider.class);
        jpa = mock(IdentityProviderStorageProvider.class);
        context = mock(KeycloakContext.class);
        log = mock(Logger.class);
        when(session.realms()).thenReturn(realms);
        when(session.clients()).thenReturn(clients);
        when(session.identityProviders()).thenReturn(providers);
        when(session.getProvider(IdentityProviderStorageProvider.class, "jpa")).thenReturn(jpa);
        when(session.getContext()).thenReturn(context);
        realm = realm("corp", true);
        adminRealm = mock(RealmModel.class);
        when(adminRealm.getId()).thenReturn(ADMIN_REALM_ID);
        when(realms.getRealmByName("admin")).thenReturn(adminRealm);
        stored = provider(null, true);
        when(jpa.getById("idp-internal")).thenReturn(stored);
        listener = new CatenaTierBindingListener(session, "exempt-client", "admin", log);
    }

    private RealmModel realm(String id, boolean holdsFlow) {
        RealmModel r = mock(RealmModel.class);
        when(r.getId()).thenReturn(id);
        when(realms.getRealm(id)).thenReturn(r);
        if (holdsFlow) {
            AuthenticationFlowModel flow = new AuthenticationFlowModel();
            flow.setId(FLOW_ID);
            when(r.getFlowByAlias(CatenaTierBindingListener.FLOW_ALIAS)).thenReturn(flow);
        }
        return r;
    }

    private static IdentityProviderModel provider(String flowId, boolean enabled) {
        IdentityProviderModel m = new IdentityProviderModel();
        m.setInternalId("idp-internal");
        m.setAlias("idp-alias");
        m.setEnabled(enabled);
        m.setPostBrokerLoginFlowId(flowId);
        m.setOrganizationIds(new LinkedHashSet<>(List.of("org-one", "org-two")));
        return m;
    }

    /** The cache layer's copy: it reports a provider linked to organizations as disabled. */
    private static IdentityProviderModel cached(String flowId) {
        return provider(flowId, false);
    }

    private static AdminEvent event(ResourceType type, OperationType op, String path,
            String authRealmId, String authClientId) {
        AdminEvent e = new AdminEvent();
        e.setRealmId("corp");
        e.setResourceType(type);
        e.setOperationType(op);
        e.setResourcePath(path);
        AuthDetails auth = new AuthDetails();
        auth.setRealmId(authRealmId);
        auth.setClientId(authClientId);
        e.setAuthDetails(auth);
        return e;
    }

    private static AdminEvent providerEvent(OperationType op, String key, String authRealmId, String authClientId) {
        return event(ResourceType.IDENTITY_PROVIDER, op, "identity-provider/instances/" + key,
                authRealmId, authClientId);
    }

    private void exemptClientHasId(String internalId) {
        ClientModel client = mock(ClientModel.class);
        when(client.getId()).thenReturn(internalId);
        when(clients.getClientByClientId(adminRealm, "exempt-client")).thenReturn(client);
    }

    @Test
    void createBindsAndKeepsTheOrganizationLinks() {
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        verify(providers).update(stored);
        assertEquals(FLOW_ID, stored.getPostBrokerLoginFlowId());
        assertEquals(Set.of("org-one", "org-two"), stored.getOrganizationIds());
        verify(context).setRealm(realm);
    }

    @Test
    void theWrittenProviderKeepsTheEnabledStateItIsStoredWith() {
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        ArgumentCaptor<IdentityProviderModel> saved = ArgumentCaptor.forClass(IdentityProviderModel.class);
        verify(providers).update(saved.capture());
        assertTrue(saved.getValue().isEnabled());
    }

    @Test
    void updateByInternalIdBinds() {
        exemptClientHasId("exempt-internal");
        when(providers.getByIdOrAlias("idp-internal")).thenReturn(cached(""));

        listener.onEvent(providerEvent(OperationType.UPDATE, "idp-internal", ADMIN_REALM_ID, "other-internal"),
                false);

        verify(providers).update(stored);
        assertEquals(FLOW_ID, stored.getPostBrokerLoginFlowId());
    }

    @Test
    void partialImportOfAProviderBinds() {
        when(providers.getByIdOrAlias("idp-internal")).thenReturn(cached(null));

        listener.onEvent(event(ResourceType.REALM, OperationType.CREATE,
                "identity-provider-settings/idp-internal", ADMIN_REALM_ID, "caller"), false);
        listener.onEvent(event(ResourceType.REALM, OperationType.UPDATE,
                "identity-provider-settings/idp-internal", ADMIN_REALM_ID, "caller"), false);

        verify(providers, org.mockito.Mockito.times(2)).update(stored);
        assertEquals(FLOW_ID, stored.getPostBrokerLoginFlowId());
    }

    @Test
    void otherRealmEventsChangeNothing() {
        listener.onEvent(event(ResourceType.REALM, OperationType.UPDATE, "", ADMIN_REALM_ID, "caller"), false);
        listener.onEvent(event(ResourceType.REALM, OperationType.UPDATE,
                "client-settings/x", ADMIN_REALM_ID, "caller"), false);

        verify(providers, never()).getByIdOrAlias(anyString());
        verify(providers, never()).update(any());
    }

    @Test
    void aProviderAlreadyNamingAFlowIsLeft() {
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached("hand-made-flow"));

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        verify(providers, never()).update(any());
        verify(jpa, never()).getById(anyString());
    }

    @Test
    void anUpdateByTheExemptClientIsLeft() {
        exemptClientHasId("exempt-internal");

        listener.onEvent(providerEvent(OperationType.UPDATE, "idp-alias", ADMIN_REALM_ID, "exempt-internal"),
                false);

        verify(providers, never()).update(any());
        verify(providers, never()).getByIdOrAlias(anyString());
    }

    @Test
    void aSameNamedClientInAnotherRealmIsBound() {
        exemptClientHasId("exempt-internal");
        RealmModel other = mock(RealmModel.class);
        when(other.getId()).thenReturn("other-realm-id");
        ClientModel sameName = mock(ClientModel.class);
        when(sameName.getId()).thenReturn("other-internal");
        when(clients.getClientByClientId(other, "exempt-client")).thenReturn(sameName);
        when(realms.getRealm("other-realm-id")).thenReturn(other);
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.UPDATE, "idp-alias", "other-realm-id", "other-internal"),
                false);

        verify(providers).update(stored);
    }

    @Test
    void withoutAnExemptClientNothingIsExempt() {
        listener = new CatenaTierBindingListener(session, "", "admin", log);
        exemptClientHasId("exempt-internal");
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.UPDATE, "idp-alias", ADMIN_REALM_ID, "exempt-internal"),
                false);

        verify(providers).update(stored);
    }

    @Test
    void thePreviousContextRealmIsRestored() {
        RealmModel previous = mock(RealmModel.class);
        when(context.getRealm()).thenReturn(previous);
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        org.mockito.InOrder order = org.mockito.Mockito.inOrder(context);
        order.verify(context).setRealm(realm);
        order.verify(context).setRealm(previous);
    }

    @Test
    void aRealmWithoutTheFlowChangesNothing() {
        realm = realm("corp", false);
        when(providers.getByIdOrAlias("idp-alias")).thenReturn(cached(null));

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        verify(providers, never()).update(any());
    }

    @Test
    void aMapperEventChangesNothing() {
        listener.onEvent(event(ResourceType.IDENTITY_PROVIDER_MAPPER, OperationType.CREATE,
                "identity-provider/instances/idp-alias/mappers/m1", ADMIN_REALM_ID, "caller"), false);

        verify(providers, never()).update(any());
        verify(providers, never()).getByIdOrAlias(anyString());
    }

    @Test
    void aDeleteChangesNothing() {
        listener.onEvent(providerEvent(OperationType.DELETE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        verify(providers, never()).update(any());
    }

    @Test
    void aThrowIsCaughtAndLogged() {
        RuntimeException boom = new RuntimeException("boom");
        when(providers.getByIdOrAlias("idp-alias")).thenThrow(boom);

        listener.onEvent(providerEvent(OperationType.CREATE, "idp-alias", ADMIN_REALM_ID, "caller"), false);

        verify(log).error(anyString(), eq(boom));
    }

    @Test
    void theSweepBindsOnlyUnboundProvidersInRealmsHoldingTheFlow() {
        RealmModel withFlow = realm("corp", true);
        RealmModel without = realm("plain", false);
        when(realms.getRealmsStream()).thenReturn(Stream.of(withFlow, without));
        when(providers.getAllStream(any(IdentityProviderQuery.class), any(), any()))
                .thenReturn(Stream.of(cached("")));
        KeycloakSessionFactory factory = mock(KeycloakSessionFactory.class);

        CatenaTierBindingListenerFactory.sweep(factory, (f, task) -> task.run(session));

        ArgumentCaptor<IdentityProviderQuery> query = ArgumentCaptor.forClass(IdentityProviderQuery.class);
        verify(providers).getAllStream(query.capture(), any(), any());
        Map<String, String> options = query.getValue().getOptions();
        assertEquals("", options.get(IdentityProviderModel.POST_BROKER_LOGIN_FLOW_ID));
        assertEquals(1, options.size());
        verify(providers).update(stored);
        assertEquals(FLOW_ID, stored.getPostBrokerLoginFlowId());
        assertTrue(stored.isEnabled());
    }

    @Test
    void theSweepSkipsAProviderAlreadyNamingAFlow() {
        when(providers.getAllStream(any(IdentityProviderQuery.class), any(), any()))
                .thenReturn(Stream.of(cached("hand-made-flow")));

        CatenaTierBindingListener.bindUnbound(session, "corp");

        verify(providers, never()).update(any());
    }
}
