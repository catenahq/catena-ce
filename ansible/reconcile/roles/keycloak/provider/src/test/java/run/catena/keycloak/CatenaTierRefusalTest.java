package run.catena.keycloak;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Set;

import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.events.Errors;
import org.keycloak.events.EventBuilder;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.AuthenticationExecutionModel.Requirement;

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

    @Test
    void authenticateRecordsTheEventAndAnswers401() {
        AuthenticationFlowContext context = mock(AuthenticationFlowContext.class);
        EventBuilder event = mock(EventBuilder.class);
        LoginFormsProvider form = mock(LoginFormsProvider.class);
        Response page = mock(Response.class);
        when(context.getEvent()).thenReturn(event);
        when(context.form()).thenReturn(form);
        when(form.setError(any(String.class))).thenReturn(form);
        when(form.createErrorPage(Response.Status.UNAUTHORIZED)).thenReturn(page);

        factory.create(null).authenticate(context);

        verify(event).error(Errors.ACCESS_DENIED);
        verify(form).setError("access-denied");
        verify(context).failure(AuthenticationFlowError.ACCESS_DENIED, page);
        assertEquals("catena-tier-refusal", factory.getId());
    }
}
