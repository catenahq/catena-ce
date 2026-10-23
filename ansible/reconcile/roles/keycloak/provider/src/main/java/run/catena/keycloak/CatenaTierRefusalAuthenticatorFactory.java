package run.catena.keycloak;

import java.util.List;

import org.keycloak.Config;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.AuthenticatorFactory;
import org.keycloak.models.AuthenticationExecutionModel.Requirement;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.provider.ProviderConfigProperty;

/**
 * Registers the tier refusal step. Keycloak's brute-force guard counts a
 * failed step unless its factory names a reference category outside password,
 * otp and recovery-authn-codes; the category here is that other one.
 */
public class CatenaTierRefusalAuthenticatorFactory implements AuthenticatorFactory {

    public static final String PROVIDER_ID = "catena-tier-refusal";
    static final String REFERENCE_CATEGORY = "catena-tier";

    private static final CatenaTierRefusalAuthenticator SINGLETON = new CatenaTierRefusalAuthenticator();
    private static final Requirement[] REQUIREMENT_CHOICES = {Requirement.REQUIRED, Requirement.DISABLED};

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public Authenticator create(KeycloakSession session) {
        return SINGLETON;
    }

    @Override
    public String getDisplayType() {
        return "Catena tier refusal";
    }

    @Override
    public String getReferenceCategory() {
        return REFERENCE_CATEGORY;
    }

    @Override
    public boolean isConfigurable() {
        return false;
    }

    @Override
    public Requirement[] getRequirementChoices() {
        return REQUIREMENT_CHOICES;
    }

    @Override
    public boolean isUserSetupAllowed() {
        return false;
    }

    @Override
    public String getHelpText() {
        return "Refuses the sign-in like Deny access, without counting toward the brute-force lockout.";
    }

    @Override
    public List<ProviderConfigProperty> getConfigProperties() {
        return List.of();
    }

    @Override
    public void init(Config.Scope config) {
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
    }

    @Override
    public void close() {
    }
}
