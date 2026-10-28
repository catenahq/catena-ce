package run.catena.keycloak;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Set;
import java.util.stream.Stream;

import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.events.Errors;
import org.keycloak.events.EventBuilder;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.AuthenticationExecutionModel.Requirement;
import org.keycloak.models.ClientModel;
import org.keycloak.models.GroupModel;
import org.keycloak.models.UserModel;
import org.keycloak.sessions.AuthenticationSessionModel;

class CatenaTierRefusalTest {

    private final CatenaTierRefusalAuthenticatorFactory factory = new CatenaTierRefusalAuthenticatorFactory();

    @Test
    void categoryIsNotNullAndOutsideTheCountedOnes() {
        String category = factory.getReferenceCategory();
        assertNotNull(category);
        assertFalse(Set.of("password", "otp", "recovery-authn-codes").contains(category));
    }

    @Test
    void hasNoConfiguration() {
        assertFalse(factory.isConfigurable());
        assertTrue(factory.getConfigProperties().isEmpty());
    }

    @Test
    void offersRequiredAndDisabledOnly() {
        assertArrayEquals(new Requirement[] {Requirement.REQUIRED, Requirement.DISABLED},
                factory.getRequirementChoices());
        assertFalse(factory.isUserSetupAllowed());
        assertFalse(factory.create(null).requiresUser());
    }

    private static GroupModel group(String name, String parentId) {
        GroupModel group = mock(GroupModel.class);
        when(group.getName()).thenReturn(name);
        when(group.getParentId()).thenReturn(parentId);
        return group;
    }

    private static AuthenticationFlowContext context(String accessGroups, GroupModel... memberOf) {
        AuthenticationFlowContext context = mock(AuthenticationFlowContext.class);
        AuthenticationSessionModel session = mock(AuthenticationSessionModel.class);
        ClientModel client = mock(ClientModel.class);
        UserModel user = mock(UserModel.class);
        when(context.getAuthenticationSession()).thenReturn(session);
        when(session.getClient()).thenReturn(client);
        when(client.getAttribute("catena.access.groups")).thenReturn(accessGroups);
        when(context.getUser()).thenReturn(user);
        when(user.getGroupsStream()).thenReturn(Stream.of(memberOf));
        return context;
    }

    private static Response refuse(AuthenticationFlowContext context) {
        EventBuilder event = mock(EventBuilder.class);
        LoginFormsProvider form = mock(LoginFormsProvider.class);
        Response page = mock(Response.class);
        when(context.getEvent()).thenReturn(event);
        when(context.form()).thenReturn(form);
        when(form.setError(any(String.class))).thenReturn(form);
        when(form.createErrorPage(Response.Status.UNAUTHORIZED)).thenReturn(page);
        return page;
    }

    private void assertRefused(AuthenticationFlowContext context) {
        Response page = refuse(context);

        factory.create(null).authenticate(context);

        verify(context.getEvent()).error(Errors.ACCESS_DENIED);
        verify(context.form()).setError("access-denied");
        verify(context).failure(AuthenticationFlowError.ACCESS_DENIED, page);
        verify(context, never()).success();
    }

    @Test
    void authenticateRecordsTheEventAndAnswers401() {
        assertRefused(context(null));
        assertEquals("catena-tier-refusal", factory.getId());
    }

    @Test
    void aMemberOfAListedGroupIsAdmitted() {
        AuthenticationFlowContext context = context("erp-user, erp-admin", group("erp-admin", null));

        factory.create(null).authenticate(context);

        verify(context).success();
        verify(context, never()).failure(any(AuthenticationFlowError.class), any(Response.class));
    }

    @Test
    void aNonMemberIsRefused() {
        assertRefused(context("erp-user,erp-admin", group("staff", null)));
    }

    @Test
    void anEmptyAttributeIsRefused() {
        assertRefused(context("", group("erp-user", null)));
        assertRefused(context(" , ,", group("", null)));
    }

    @Test
    void aMissingAttributeIsRefused() {
        assertRefused(context(null, group("erp-user", null)));
    }

    @Test
    void aGroupOfTheSameNameUnderAnotherGroupIsRefused() {
        assertRefused(context("erp-user", group("erp-user", "some-parent-id")));
    }

    @Test
    void aSignInWithoutAUserOrAClientIsRefused() {
        AuthenticationFlowContext noUser = context("erp-user");
        when(noUser.getUser()).thenReturn(null);
        assertRefused(noUser);

        AuthenticationFlowContext noClient = context("erp-user", group("erp-user", null));
        when(noClient.getAuthenticationSession().getClient()).thenReturn(null);
        assertRefused(noClient);
    }
}
