package run.catena.keycloak;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.jboss.logging.Logger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.OperationType;
import org.keycloak.events.admin.ResourceType;
import org.keycloak.models.ClientModel;
import org.keycloak.models.ClientScopeModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RealmProvider;

class CatenaAdminApiScopeTest {

    private static final Map<String, Set<String>> NAMED = CatenaAdminApiScopeListenerFactory.parse(
            "master/catena-automation,vps/dashboard-sync,vps/catena-admin-panel");

    private KeycloakSession session;
    private RealmProvider realms;
    private Logger log;
    private ClientScopeModel scope;
    private RealmModel vps;
    private ClientModel sync;
    private ClientModel panel;
    private ClientModel other;
    private CatenaAdminApiScopeListener listener;

    @BeforeEach
    void setUp() {
        session = mock(KeycloakSession.class);
        realms = mock(RealmProvider.class);
        log = mock(Logger.class);
        when(session.realms()).thenReturn(realms);
        scope = scope(CatenaAdminApiScopeListener.SCOPE);
        vps = realm("vps-id", "vps", true);
        sync = client(vps, "dashboard-sync");
        panel = client(vps, "catena-admin-panel");
        other = client(vps, "app-other");
        listener = new CatenaAdminApiScopeListener(session, NAMED, log);
    }

    private static ClientScopeModel scope(String name) {
        ClientScopeModel s = mock(ClientScopeModel.class);
        when(s.getName()).thenReturn(name);
        return s;
    }

    private RealmModel realm(String id, String name, boolean holdsScope) {
        RealmModel r = mock(RealmModel.class);
        when(r.getId()).thenReturn(id);
        when(r.getName()).thenReturn(name);
        when(realms.getRealm(id)).thenReturn(r);
        when(realms.getRealmByName(name)).thenReturn(r);
        List<ClientScopeModel> scopes = holdsScope ? List.of(scope("profile"), scope) : List.of(scope("profile"));
        when(r.getClientScopesStream()).thenAnswer(i -> scopes.stream());
        return r;
    }

    /** A client whose default and optional scopes are maps by name, as the model keeps them. */
    private static ClientModel client(RealmModel realm, String clientId) {
        ClientModel c = mock(ClientModel.class);
        Map<String, ClientScopeModel> defaults = new HashMap<>();
        Map<String, ClientScopeModel> optional = new HashMap<>();
        when(c.getClientId()).thenReturn(clientId);
        when(c.getClientScopes(true)).thenReturn(defaults);
        when(c.getClientScopes(false)).thenReturn(optional);
        doAnswer(i -> {
            ClientScopeModel s = i.getArgument(0);
            ((boolean) i.getArgument(1) ? defaults : optional).put(s.getName(), s);
            return null;
        }).when(c).addClientScope(any(), anyBoolean());
        when(realm.getClientByClientId(clientId)).thenReturn(c);
        return c;
    }

    private static AdminEvent event(String realmId, ResourceType type, OperationType op, String path) {
        AdminEvent e = new AdminEvent();
        e.setRealmId(realmId);
        e.setResourceType(type);
        e.setOperationType(op);
        e.setResourcePath(path);
        return e;
    }

    @Test
    void theOptionNamesClientsByRealm() {
        assertEquals(Map.of("master", Set.of("catena-automation"), "vps", Set.of("dashboard-sync", "catena-admin-panel")),
                NAMED);
        assertEquals(Map.of("vps", Set.of("a/b", "c")),
                CatenaAdminApiScopeListenerFactory.parse(" vps/a/b , ,novalue,/x,y/,vps/c"));
        assertTrue(CatenaAdminApiScopeListenerFactory.parse(null).isEmpty());
    }

    @Test
    void aNamedClientGetsTheScopeAsADefaultScopeOnce() {
        CatenaAdminApiScopeListener.grant(vps, NAMED.get("vps"), log);
        CatenaAdminApiScopeListener.grant(vps, NAMED.get("vps"), log);

        verify(sync, times(1)).addClientScope(any(), anyBoolean());
        verify(sync).addClientScope(scope, true);
        verify(panel, times(1)).addClientScope(any(), anyBoolean());
        verify(panel).addClientScope(scope, true);
    }

    @Test
    void aClientHoldingTheScopeAlreadyIsLeft() {
        sync.getClientScopes(true).put(CatenaAdminApiScopeListener.SCOPE, scope);
        panel.getClientScopes(false).put(CatenaAdminApiScopeListener.SCOPE, scope);

        CatenaAdminApiScopeListener.grant(vps, NAMED.get("vps"), log);

        verify(sync, never()).addClientScope(any(), anyBoolean());
        verify(panel, never()).addClientScope(any(), anyBoolean());
    }

    @Test
    void aRealmWithoutTheScopeIsLeftAlone() {
        RealmModel old = realm("old-id", "vps", false);
        ClientModel oldSync = client(old, "dashboard-sync");
        // A client named like the scope is never taken for it.
        client(old, CatenaAdminApiScopeListener.SCOPE);

        CatenaAdminApiScopeListener.grant(old, NAMED.get("vps"), log);

        verify(oldSync, never()).addClientScope(any(), anyBoolean());
        verify(old, never()).getClientByClientId(anyString());
    }

    @Test
    void aClientNotNamedIsNeverTouched() {
        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.CREATE, "clients/app-other-id"), false);

        verify(other, never()).addClientScope(any(), anyBoolean());
        verify(other, never()).getClientScopes(anyBoolean());
        verify(vps, never()).getClientByClientId("app-other");
    }

    @Test
    void aClientNamedForAnotherRealmIsNeverTouched() {
        RealmModel master = realm("master-id", "master", true);
        ClientModel sameName = client(master, "dashboard-sync");
        ClientModel automation = client(master, "catena-automation");

        listener.onEvent(event("master-id", ResourceType.CLIENT, OperationType.UPDATE, "clients/x"), false);

        verify(sameName, never()).addClientScope(any(), anyBoolean());
        verify(automation).addClientScope(scope, true);
    }

    @Test
    void aClientCreateGrantsTheRealmsNamedClients() {
        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.CREATE, "clients/sync-id"), false);

        verify(sync).addClientScope(scope, true);
        verify(panel).addClientScope(scope, true);
    }

    @Test
    void anUpdateThatDroppedTheScopeGivesItBack() {
        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.CREATE, "clients/sync-id"), false);
        sync.getClientScopes(true).clear();

        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.UPDATE, "clients/sync-id"), false);

        verify(sync, times(2)).addClientScope(scope, true);
        verify(panel, times(1)).addClientScope(scope, true);
    }

    @Test
    void aPartialImportOfAClientGrants() {
        listener.onEvent(event("vps-id", ResourceType.REALM, OperationType.CREATE, "clients/sync-id"), false);

        verify(sync).addClientScope(scope, true);
    }

    @Test
    void otherEventsChangeNothing() {
        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.DELETE, "clients/sync-id"), false);
        listener.onEvent(event("vps-id", ResourceType.CLIENT_SCOPE_CLIENT_MAPPING, OperationType.DELETE,
                "clients/sync-id/default-client-scopes/s"), false);
        listener.onEvent(event("vps-id", ResourceType.REALM, OperationType.UPDATE, ""), false);
        listener.onEvent(event("vps-id", ResourceType.REALM, OperationType.CREATE, "users/u"), false);
        listener.onEvent(event("vps-id", ResourceType.USER, OperationType.CREATE, "users/u"), false);

        verify(sync, never()).addClientScope(any(), anyBoolean());
        verify(realms, never()).getRealm(anyString());
    }

    @Test
    void aRealmNamingNoClientsChangesNothing() {
        RealmModel corp = realm("corp-id", "corp", true);
        ClientModel corpSync = client(corp, "dashboard-sync");

        listener.onEvent(event("corp-id", ResourceType.CLIENT, OperationType.CREATE, "clients/x"), false);
        listener.onEvent(event("gone-id", ResourceType.CLIENT, OperationType.CREATE, "clients/x"), false);

        verify(corpSync, never()).addClientScope(any(), anyBoolean());
        verify(log, never()).error(anyString(), any(Throwable.class));
    }

    @Test
    void aThrowIsCaughtAndLogged() {
        RuntimeException boom = new RuntimeException("boom");
        when(vps.getClientScopesStream()).thenThrow(boom);

        listener.onEvent(event("vps-id", ResourceType.CLIENT, OperationType.CREATE, "clients/x"), false);

        verify(log).error(anyString(), eq(boom));
    }

    @Test
    void theSweepGrantsEveryNamedRealmInItsOwnTransaction() {
        RealmModel master = realm("master-id", "master", true);
        ClientModel automation = client(master, "catena-automation");
        KeycloakSessionFactory factory = mock(KeycloakSessionFactory.class);
        int[] runs = {0};

        CatenaAdminApiScopeListenerFactory.sweep(factory, (f, task) -> {
            runs[0]++;
            task.run(session);
        }, NAMED);

        assertEquals(2, runs[0]);
        verify(automation).addClientScope(scope, true);
        verify(sync).addClientScope(scope, true);
        verify(panel).addClientScope(scope, true);
        verify(other, never()).addClientScope(any(), anyBoolean());
    }

    @Test
    void aSweepFailureInOneRealmLeavesTheOthers() {
        when(realms.getRealmByName("master")).thenThrow(new RuntimeException("boom"));
        KeycloakSessionFactory factory = mock(KeycloakSessionFactory.class);

        CatenaAdminApiScopeListenerFactory.sweep(factory, (f, task) -> task.run(session), NAMED);

        verify(sync).addClientScope(scope, true);
    }

    @Test
    void theSweepSkipsAMissingRealm() {
        KeycloakSessionFactory factory = mock(KeycloakSessionFactory.class);

        CatenaAdminApiScopeListenerFactory.sweep(factory, (f, task) -> task.run(session),
                CatenaAdminApiScopeListenerFactory.parse("gone/dashboard-sync"));

        verify(sync, never()).addClientScope(any(), anyBoolean());
    }
}
